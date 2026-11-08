# -*- coding: utf-8 -*-
"""视频离线检测：逐帧推理、边写标注视频、进度写回数据库。

**与旧桌面端实现的关键差异**：
旧代码把每一帧都 append 进 `self.saved_images`，640×640×3 ≈ 1.17 MB/帧，
按 30fps 计算约 **35 MB/s**，10 分钟就吃掉约 21 GB 内存。
这里改为「处理一帧就写一帧」，内存占用与视频长度无关。

**中文路径的处理**：OpenCV 的 `VideoCapture` / `VideoWriter` 在 Windows 上
走窄字符 API，项目路径含「病虫害」时会直接打不开。这里采用
「先试直连，失败则退到 ASCII 临时文件」的策略 —— 既不牺牲常见情况的性能，
又保证中文路径下依然可用。图像读写已在 `imaging.py` 用 imencode/imdecode 解决。
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..config import Settings
from ..models import Detection as DetectionRow
from ..models import Task, TaskStatus, utcnow
from .alerts import evaluate_task
from .detector import DetectorService
from .imaging import draw_detections
from .storage import task_dir, url_for_output
from .taskmeta import detection_to_row_kwargs, dump_counts
from .tracker import IouTracker

# 明细入库上限：长视频的框数可达几十万行，抽样保留前若干行即可满足回放与抽查
DEFAULT_DETECTION_ROW_LIMIT = 20_000
# 每处理多少帧把进度刷回数据库（同时也是前端进度条的刷新粒度）
PROGRESS_EVERY = 20


def _open_capture(path: Path) -> tuple[Any, Path | None]:
    """打开视频。返回 (capture, 需要清理的临时目录)。

    非 ASCII 路径失败时，先复制到 ASCII 临时目录再打开。
    """
    capture = cv2.VideoCapture(str(path))
    if capture.isOpened():
        return capture, None

    capture.release()
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="insect_in_"))
        tmp_path = tmp_dir / f"src{path.suffix or '.mp4'}"
        shutil.copyfile(str(path), str(tmp_path))
    except OSError:
        return None, None

    capture = cv2.VideoCapture(str(tmp_path))
    if not capture.isOpened():
        capture.release()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None
    return capture, tmp_dir


class _VideoSink:
    """标注视频写出。优先直写目标路径，失败则写 ASCII 临时文件再搬回。"""

    def __init__(self, target: Path, fps: float, size: tuple[int, int]) -> None:
        self.target = target
        self._tmp_dir: Path | None = None
        self._tmp_path: Path | None = None
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")

        self.writer = cv2.VideoWriter(str(target), fourcc, fps, size)
        if not self.writer.isOpened():
            self.writer.release()
            self._tmp_dir = Path(tempfile.mkdtemp(prefix="insect_out_"))
            self._tmp_path = self._tmp_dir / "out.mp4"
            self.writer = cv2.VideoWriter(str(self._tmp_path), fourcc, fps, size)

    @property
    def ok(self) -> bool:
        return bool(self.writer.isOpened())

    def write(self, frame: np.ndarray) -> None:
        self.writer.write(frame)

    def close(self) -> None:
        try:
            self.writer.release()
        except Exception:  # noqa: BLE001
            pass
        if self._tmp_dir is not None and self._tmp_path is not None:
            try:
                self.target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(self._tmp_path), str(self.target))
            except OSError:
                pass
            shutil.rmtree(self._tmp_dir, ignore_errors=True)


def process_video_task(
    *,
    session_factory: Any,
    settings: Settings,
    detector: DetectorService,
    task_id: int,
    video_path: str | Path,
    conf: float,
    iou: float,
    stride: int = 1,
    detection_row_limit: int = DEFAULT_DETECTION_ROW_LIMIT,
) -> None:
    """在调用线程里同步跑完一个视频任务（调用方负责放进线程池）。

    自己不创建线程也不抛异常 —— 失败一律写进 task.error，
    因为这是后台任务，没人会接住异常。
    """
    db = session_factory()
    started = time.perf_counter()
    sink: _VideoSink | None = None
    cleanup_dir: Path | None = None

    try:
        task = db.get(Task, task_id)
        if task is None:
            return
        task.status = TaskStatus.RUNNING
        db.commit()

        capture, cleanup_dir = _open_capture(Path(video_path))
        if capture is None:
            task.status = TaskStatus.FAILED
            task.error = "无法打开视频（编码不受支持、文件损坏或路径不可访问）"
            task.finished_at = utcnow()
            db.commit()
            return

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 1.0 or fps > 240.0:
            fps = 25.0
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        task.total_frames = total_frames or None
        db.commit()

        tracker = IouTracker(
            iou_thresh=settings.tracking.iou_thresh, max_age=settings.tracking.max_age
        )
        counts: dict[str, int] = {}
        frame_index = 0
        rows_written = 0
        total_objects = 0
        out_path = task_dir(settings, task_id) / "annotated.mp4"
        step = max(1, int(stride))

        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frame_index += 1
            if step > 1 and (frame_index % step) != 0:
                continue

            result = detector.infer(frame, conf, iou)
            detections = result.detections

            if settings.tracking.enabled:
                tracker.update(detections)
            total_objects += len(detections)
            for det in detections:
                name = det.zh_name or det.class_name
                counts[name] = counts.get(name, 0) + 1

            # 明细抽样入库，避免长视频把表撑爆（聚合计数单独存放，不受影响）
            if rows_written < detection_row_limit:
                for det in detections:
                    db.add(DetectionRow(**detection_to_row_kwargs(det, task_id, frame_index)))
                    rows_written += 1

            annotated = draw_detections(frame, detections) if detections else frame
            if sink is None:
                height, width = annotated.shape[:2]
                sink = _VideoSink(out_path, fps, (width, height))
            if sink.ok:
                sink.write(annotated)

            if frame_index % PROGRESS_EVERY == 0:
                task.frame_count = frame_index
                task.total_objects = total_objects
                task.unique_objects = tracker.unique_count if settings.tracking.enabled else 0
                db.commit()

        capture.release()

        task.frame_count = frame_index
        task.total_frames = total_frames or frame_index
        task.total_objects = total_objects
        task.unique_objects = tracker.unique_count if settings.tracking.enabled else 0
        task.counts_json = dump_counts(counts)
        task.duration_ms = int((time.perf_counter() - started) * 1000)
        task.finished_at = utcnow()
        task.status = TaskStatus.DONE
        if sink is not None and sink.ok:
            task.result_video_path = url_for_output(task_id, "annotated.mp4")
        db.commit()
        # 与其他模式一致：任务完成后评估一次告警规则
        evaluate_task(db, task)

    except Exception as exc:  # noqa: BLE001 - 后台任务必须把失败落到记录里
        try:
            task = db.get(Task, task_id)
            if task is not None:
                task.status = TaskStatus.FAILED
                task.error = f"{type(exc).__name__}: {exc}"
                task.finished_at = utcnow()
                db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if sink is not None:
            sink.close()
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        db.close()


def reset_stale_tasks(session_factory: Any) -> int:
    """把上次进程异常退出时卡在 running/pending 的任务标记为失败。

    否则重启后这些任务会永远显示「进行中」，前端进度条永远转圈。
    """
    db = session_factory()
    try:
        stale = (
            db.query(Task)
            .filter(Task.status.in_((TaskStatus.RUNNING, TaskStatus.PENDING)))
            .all()
        )
        for task in stale:
            task.status = TaskStatus.FAILED
            task.error = "服务重启导致任务中断"
            task.finished_at = utcnow()
        if stale:
            db.commit()
        return len(stale)
    finally:
        db.close()
