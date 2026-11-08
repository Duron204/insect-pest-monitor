# -*- coding: utf-8 -*-
"""检测接口：图片 / 批量（文件夹）/ 视频。

设计取舍（见实施计划 §3.4）：
- **图片与批量：同步返回，框由前端 canvas 叠加**，不把框烧进返回图。
  这样前端可以缩放/悬停/切换显示，传输量也更小；需要烧框版本时另有
  `annotated_url` 供下载与报告使用。
- **视频：转后台任务**，立即返回 `task_id`；进度从 `GET /api/tasks/{id}`
  或 WebSocket 订阅获取，避免长 HTTP 请求超时。
- 推理一律放线程池（`to_thread`），否则会阻塞事件循环 ——
  原桌面端的 WebSocket 服务 正是同步推理导致 stop 消息得不到及时处理。
"""
from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from pathlib import Path
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_current_user, get_db
from ..models import Detection as DetectionRow
from ..models import Task, TaskMode, TaskStatus, User, utcnow
from ..schemas import (
    BatchDetectionOut,
    BatchItemOut,
    DetectionOut,
    ImageDetectionOut,
    VideoAcceptedOut,
)
from ..services.alerts import evaluate_task
from ..services.detector import DetectorService
from ..services.imaging import count_by_class, decode_image, draw_detections, save_image
from ..services.storage import (
    UploadTooLarge,
    read_upload_limited,
    save_upload,
    sign_task_media,
    task_dir,
    url_for_output,
)
from ..services.taskmeta import detection_to_row_kwargs, dump_counts
from ..services.video import process_video_task

logger = logging.getLogger("insect")

router = APIRouter(prefix="/detection", tags=["detection"])


def _detector(request: Request) -> DetectorService:
    detector = getattr(request.app.state, "detector", None)
    if detector is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="推理服务未初始化"
        )
    return detector


def _resolve_thresholds(detector: DetectorService, conf: float | None, iou: float | None) -> tuple[float, float]:
    use_conf = detector.conf if conf is None else min(max(float(conf), 0.0), 1.0)
    use_iou = detector.iou if iou is None else min(max(float(iou), 0.0), 1.0)
    return use_conf, use_iou


async def _read_limited(file: UploadFile, settings: Settings) -> bytes:
    # 分块读、超限即停 —— **不能**先 read() 再比大小，那样内存早就吃完了。
    try:
        data = await read_upload_limited(file, settings.storage.max_upload_mb)
    except UploadTooLarge as exc:
        raise HTTPException(
            status_code=413,  # 直接用字面量：HTTP_413_* 常量名随 Starlette 版本变化
            detail=str(exc),
        ) from None
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件为空")
    return data


def _discard_upload(path: Path) -> None:
    """删掉已处理完的上传文件；失败只记日志，绝不影响任务结果。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - 权限/占用等边缘情况
        logger.warning("删除上传文件失败：%s（%s）", path, exc)


def _new_task(
    db: Session,
    *,
    mode: str,
    source_name: str | None,
    conf: float,
    iou: float,
    user: User,
    total_frames: int | None = None,
) -> Task:
    task = Task(
        mode=mode,
        status=TaskStatus.RUNNING,
        source_name=source_name,
        conf=conf,
        iou=iou,
        user_id=user.id,
        total_frames=total_frames,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ---------------------------------------------------------------- 单图
@router.post("/image", response_model=ImageDetectionOut)
async def detect_image(
    request: Request,
    file: UploadFile = File(...),
    conf: float | None = Form(None),
    iou: float | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImageDetectionOut:
    settings: Settings = request.app.state.settings
    detector = _detector(request)

    data = await _read_limited(file, settings)
    image = decode_image(data)
    if image is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不是有效的图片文件（支持 jpg/jpeg/png/bmp/webp）",
        )

    use_conf, use_iou = _resolve_thresholds(detector, conf, iou)
    task = _new_task(
        db,
        mode=TaskMode.IMAGE,
        source_name=file.filename,
        conf=use_conf,
        iou=use_iou,
        user=user,
        total_frames=1,
    )

    started = time.perf_counter()
    result = await to_thread.run_sync(lambda: detector.infer(image, use_conf, use_iou))
    detections = result.detections

    directory = task_dir(settings, task.id)
    save_image(directory / "original.jpg", image)
    save_image(directory / "annotated.jpg", draw_detections(image, detections))

    for det in detections:
        db.add(DetectionRow(**detection_to_row_kwargs(det, task.id, 1)))

    counts = count_by_class(detections)
    height, width = image.shape[:2]

    task.status = TaskStatus.DONE
    task.frame_count = 1
    task.total_objects = len(detections)
    # 单张图像内每个框都是独立个体，去重数即框数
    task.unique_objects = len(detections)
    task.counts_json = dump_counts(counts)
    task.duration_ms = int((time.perf_counter() - started) * 1000)
    task.result_image_path = url_for_output(task.id, "annotated.jpg")
    task.finished_at = utcnow()
    db.commit()
    # 与设备上报路径保持一致：任务完成后评估一次告警规则
    evaluate_task(db, task)

    # 库里的 `result_image_path` 仍是裸地址（见 storage.py），只有**回给浏览器**的
    # 地址才带票据 —— 这是上传后立刻回显的那两张图，漏签就是破图。
    return ImageDetectionOut(
        task_id=task.id,
        image_url=sign_task_media(
            url_for_output(task.id, "original.jpg"),
            task_id=task.id, user_id=user.id, settings=settings,
        ),
        annotated_url=sign_task_media(
            url_for_output(task.id, "annotated.jpg"),
            task_id=task.id, user_id=user.id, settings=settings,
        ),
        width=width,
        height=height,
        detections=[DetectionOut(**det.to_dict()) for det in detections],
        counts=counts,
        frame_total=len(detections),
        unique_objects=len(detections),
        inference_ms=round(result.inference_ms, 2),
    )


# ---------------------------------------------------------------- 批量（文件夹）
@router.post("/batch", response_model=BatchDetectionOut)
async def detect_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    conf: float | None = Form(None),
    iou: float | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BatchDetectionOut:
    settings: Settings = request.app.state.settings
    detector = _detector(request)

    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未收到任何文件")
    if len(files) > settings.storage.max_batch_files:
        raise HTTPException(
            status_code=413,  # 直接用字面量：HTTP_413_* 常量名随 Starlette 版本变化
            detail=f"单次最多 {settings.storage.max_batch_files} 个文件",
        )

    use_conf, use_iou = _resolve_thresholds(detector, conf, iou)
    task = _new_task(
        db,
        mode=TaskMode.FOLDER,
        source_name=f"{len(files)} 个文件",
        conf=use_conf,
        iou=use_iou,
        user=user,
        total_frames=len(files),
    )

    started = time.perf_counter()
    directory = task_dir(settings, task.id) / "batch"
    directory.mkdir(parents=True, exist_ok=True)

    items: list[BatchItemOut] = []
    aggregate: dict[str, int] = {}
    total_objects = 0
    succeeded = 0
    failed = 0

    for index, upload in enumerate(files):
        original_name = upload.filename or f"file_{index}"
        entry = BatchItemOut(filename=original_name)
        try:
            # 同样分块读：一张超限的图不该先把内存吃光再报错。
            # `UploadTooLarge` 是 `ValueError`，由下面那个 `except Exception` 接住，
            # 错误文本与原来逐字一致。
            data = await read_upload_limited(upload, settings.storage.max_upload_mb)
            if not data:
                raise ValueError("文件为空")

            image = decode_image(data)
            if image is None:
                raise ValueError("不是有效的图片文件")

            result = await to_thread.run_sync(
                lambda img=image: detector.infer(img, use_conf, use_iou)
            )
            detections = result.detections

            stem = f"{index:03d}_{Path(original_name).stem[:40] or 'image'}"
            save_image(directory / f"{stem}_orig.jpg", image)
            save_image(directory / f"{stem}_anno.jpg", draw_detections(image, detections))

            for det in detections:
                db.add(DetectionRow(**detection_to_row_kwargs(det, task.id, index + 1)))

            counts = count_by_class(detections)
            for name, value in counts.items():
                aggregate[name] = aggregate.get(name, 0) + value
            total_objects += len(detections)

            height, width = image.shape[:2]
            # 嵌套路径（`batch/...`）同样由媒体闸门按第一段任务号鉴权，
            # 票据只看任务、不看文件层级 —— 见 media_guard.py。
            entry.image_url = sign_task_media(
                url_for_output(task.id, f"batch/{stem}_orig.jpg"),
                task_id=task.id, user_id=user.id, settings=settings,
            )
            entry.width = width
            entry.height = height
            entry.detections = [DetectionOut(**det.to_dict()) for det in detections]
            entry.counts = counts
            entry.frame_total = len(detections)
            entry.inference_ms = round(result.inference_ms, 2)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001 - 单张失败不应中断整批
            entry.error = str(exc)
            failed += 1
        items.append(entry)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    task.frame_count = len(files)
    task.total_objects = total_objects
    task.unique_objects = total_objects  # 每张图独立，不做跨图跟踪
    task.counts_json = dump_counts(aggregate)
    task.duration_ms = elapsed_ms
    task.finished_at = utcnow()
    task.status = TaskStatus.DONE if succeeded else TaskStatus.FAILED
    if failed:
        task.error = f"{failed} 个文件处理失败"
    db.commit()
    evaluate_task(db, task)

    return BatchDetectionOut(
        task_id=task.id,
        total=len(files),
        succeeded=succeeded,
        failed=failed,
        total_objects=total_objects,
        counts=aggregate,
        items=items,
        elapsed_ms=elapsed_ms,
    )


# ---------------------------------------------------------------- 视频
@router.post("/video", response_model=VideoAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def detect_video(
    request: Request,
    file: UploadFile = File(...),
    conf: float | None = Form(None),
    iou: float | None = Form(None),
    stride: int = Form(1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VideoAcceptedOut:
    settings: Settings = request.app.state.settings
    detector = _detector(request)

    data = await _read_limited(file, settings)
    saved_path = save_upload(settings, file.filename, data)

    use_conf, use_iou = _resolve_thresholds(detector, conf, iou)
    task = _new_task(
        db,
        mode=TaskMode.VIDEO,
        source_name=file.filename,
        conf=use_conf,
        iou=use_iou,
        user=user,
    )

    # 后台执行。必须持有返回的 asyncio.Task 引用，否则可能被 GC 回收导致任务半途消失。
    job = asyncio.create_task(
        to_thread.run_sync(
            partial(
                process_video_task,
                session_factory=request.app.state.session_factory,
                settings=settings,
                detector=detector,
                task_id=task.id,
                video_path=saved_path,
                conf=use_conf,
                iou=use_iou,
                stride=max(1, int(stride)),
            )
        )
    )
    jobs: set[Any] = request.app.state.bg_jobs
    jobs.add(job)
    job.add_done_callback(jobs.discard)
    # 上传的原始视频处理完就删。此前 `storage.keep_uploads` 从未被读取，
    # 于是**一个 500MB 的视频会在树莓派上永久占 500MB**。
    # 上传目录不对外暴露（/media 只挂 avatars 与 outputs）、source_path 也无处读取，
    # 所以删除是安全的。
    if not settings.storage.keep_uploads:
        job.add_done_callback(lambda _job, path=saved_path: _discard_upload(path))

    return VideoAcceptedOut(
        task_id=task.id,
        status=task.status,
        source_name=task.source_name or "",
        total_frames=0,
    )
