# -*- coding: utf-8 -*-
"""检测任务（历史记录）接口：列表 / 详情 / 明细 / 删除 / 报告导出。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_current_user, get_db, get_settings
from ..models import Detection as DetectionRow
from ..models import Device, DeviceStatus, Species, Task, TaskStatus, User
from ..schemas import DetectionOut, Page, TaskDetailOut, TaskOut
from ..services.ownership import owned_or_404, owner_scope
from ..services.report import build_report
from ..services.storage import (
    remove_task_dir,
    sign_task_media,
    sign_task_media_fields,
)
from ..services.taskmeta import load_counts, task_progress

router = APIRouter(prefix="/tasks", tags=["tasks"])

_MEDIA_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
}


@router.get("", response_model=Page[TaskOut])
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    mode: str | None = Query(None, description="image/video/camera/folder/local/device"),
    status_filter: str | None = Query(None, alias="status"),
    plot_id: int | None = Query(None),
    device_id: int | None = Query(None, description="只看某台绑定设备的结果（需归你所有）"),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> Page[TaskOut]:
    # 第一道也是唯一一道读过滤：无主的公共、有主的只有主人看得见。
    # 下面那些筛选参数（mode / plot_id / since / until）都在它**之后**收敛，
    # 所以拿别人的 plot_id 或 mode 来查，只会查到空，不构成旁路。
    query = db.query(Task).filter(owner_scope(Task.user_id, user))
    if mode:
        query = query.filter(Task.mode == mode)
    if status_filter:
        query = query.filter(Task.status == status_filter)
    if plot_id is not None:
        query = query.filter(Task.plot_id == plot_id)
    if device_id is not None:
        # 归属校验。**不校验的话这个参数仍然是一条旁路**：任务列表虽然已经按账号
        # 过滤了，但"按设备过滤"会用一个明确的 0 条/404 差别告诉别人"这台设备存在"。
        # 与 routers/devices.py 的 `_owned_device_or_404` 保持同一口径：不是我的就 404。
        owned = db.get(Device, device_id)
        if owned is None or owned.owner_id != user.id or owned.status != DeviceStatus.ACTIVE:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
        query = query.filter(Task.device_id == device_id)
    if since is not None:
        query = query.filter(Task.started_at >= since.replace(tzinfo=None))
    if until is not None:
        query = query.filter(Task.started_at <= until.replace(tzinfo=None))

    total = query.count()
    rows = (
        query.order_by(Task.started_at.desc(), Task.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    # 产物地址必须带媒体票据才能被 <img> / <video> 取到（见 media_guard.py）。
    # 票据按**调用者**签发，同一个窗口内地址稳定，浏览器缓存照常生效。
    items = [
        sign_task_media_fields(
            TaskOut.model_validate(row), task_id=row.id, user_id=user.id, settings=settings
        )
        for row in rows
    ]
    return Page[TaskOut](items=items, total=total, page=page, page_size=page_size)


def _visible_task(db: Session, task_id: int, user: User) -> Task:
    """取一条**调用者看得见**的任务，否则 404。

    判据只有一条（定义在 `services/ownership.py`）：**无主的谁都能看见，
    有主的只有主人看得见**。`user_id is None` 的是本机摄像头定时抓拍
    （`services/local_camera.py` 写 NULL，它本来就没有"谁上传的"这一说）
    以及更早的历史数据 —— 它们是农场共有的。

    **读和删共用这一个判据**（看得见 = 能删）。上一版只在删除上收口、
    读是全局共享的，所以那时回 403 并解释"任务就在你的列表里"；
    现在读也收口了，别人有主的任务在任何界面上都不出现 —— 再回 403 就等于
    承认"有这么一条，只是不是你的"，接口变成按 id 枚举别人数据量的探针。
    所以统一改回 404，与 `routers/devices.py::_owned_device_or_404` 同一口径。
    """
    return owned_or_404(
        db, Task, task_id, user, owner_attr="user_id", what="任务"
    )


@router.get("/{task_id}", response_model=TaskDetailOut)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> TaskDetailOut:
    task = _visible_task(db, task_id, user)
    detail = TaskDetailOut.model_validate(task)
    detail.counts = load_counts(task)
    detail.progress = task_progress(task)
    return sign_task_media_fields(detail, task_id=task.id, user_id=user.id, settings=settings)


@router.get("/{task_id}/detections", response_model=Page[DetectionOut])
def get_task_detections(
    task_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=2000),
    frame_index: int | None = Query(None),
    class_name: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Page[DetectionOut]:
    _visible_task(db, task_id, user)
    query = db.query(DetectionRow).filter(DetectionRow.task_id == task_id)
    if frame_index is not None:
        query = query.filter(DetectionRow.frame_index == frame_index)
    if class_name:
        query = query.filter(DetectionRow.class_name == class_name)

    total = query.count()
    rows = (
        query.order_by(DetectionRow.frame_index, DetectionRow.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        DetectionOut(
            class_id=row.class_id,
            class_name=row.class_name,
            zh_name=row.class_name,
            score=row.score,
            bbox=[row.x1, row.y1, row.x2, row.y2],
            track_id=row.track_id,
            frame_index=row.frame_index,
        )
        for row in rows
    ]
    return Page[DetectionOut](items=items, total=total, page=page, page_size=page_size)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """删除任务及其全部产物（目录随任务一起清掉，避免磁盘泄漏）。

    归属校验在 `_visible_task` 里 —— 与读**完全同一条判据**（看得见 = 能删）。
    无主的（本机抓拍、旧数据）谁都能删；别人有主的在这里**看不见**，回 404。
    """
    task = _visible_task(db, task_id, user)
    settings: Settings = request.app.state.settings
    db.delete(task)          # 明细由 cascade 一起删除
    db.commit()
    # 先从库里删掉再删目录：反过来的话，remove_task_dir 失败会留下一条
    # 指向已经不存在产物的任务（磁盘泄漏变成数据不一致）。
    remove_task_dir(settings, task_id)


@router.get("/{task_id}/report")
def download_report(
    task_id: int,
    request: Request,
    format: str = Query("xlsx", pattern="^(xlsx|docx|html)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    task = _visible_task(db, task_id, user)
    counts = load_counts(task)

    detections = (
        db.query(DetectionRow)
        .filter(DetectionRow.task_id == task_id)
        .order_by(DetectionRow.frame_index, DetectionRow.id)
        .limit(500)
        .all()
    )

    # 命中的虫种 -> 知识库内容
    species: dict[str, dict] = {}
    if counts:
        rows = db.query(Species).all()
        zh_to_item = {
            row.zh_name: {
                "en_name": row.en_name,
                "host_crops": row.host_crops,
                "damage": row.damage,
                "control": row.control,
                "threshold_desc": row.threshold_desc,
            }
            for row in rows
        }
        for name in counts:
            item = zh_to_item.get(name)
            if item:
                species[name] = item

    try:
        path: Path = build_report(
            task,
            settings=request.app.state.settings,
            counts=counts,
            detections=detections,
            species=species,
            fmt=format,
        )
    except Exception as exc:  # noqa: BLE001 - 导出失败要给出可读原因
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"报告生成失败：{exc}"
        ) from exc

    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="报告文件未生成"
        )

    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(format, "application/octet-stream"),
        filename=path.name,
    )


@router.get("/{task_id}/counts")
def get_task_counts(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """轻量接口：只取分类计数，用于详情抽屉快速渲染。"""
    task = _visible_task(db, task_id, user)
    return {
        "task_id": task.id,
        "counts": load_counts(task),
        "total_objects": task.total_objects,
        "unique_objects": task.unique_objects,
        "progress": task_progress(task),
        "result_video_path": sign_task_media(
            task.result_video_path, task_id=task.id, user_id=user.id, settings=settings
        ),
        "result_image_path": sign_task_media(
            task.result_image_path, task_id=task.id, user_id=user.id, settings=settings
        ),
        "detection_rows": db.query(func.count(DetectionRow.id))
        .filter(DetectionRow.task_id == task_id)
        .scalar()
        or 0,
    }
