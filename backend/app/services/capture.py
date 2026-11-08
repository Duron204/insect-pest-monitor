# -*- coding: utf-8 -*-
"""「一帧图像 → 一条完整任务记录」的落库逻辑。

**为什么要单独成模块**：一帧任务的落库步骤很多（存原图/标注图、写框级明细、
算分类计数、更新任务字段并提交）。原先设备上报（`routers/ingest.py`，已随设备层移除）
与本机摄像头抓拍（`services/local_camera.py`）两条路共用本模块，目的是避免
"一边有标注图、另一边没有"这类不对称缺陷。现在只剩本机抓拍一个调用方，
保留独立模块是为了让抓拍服务专注于取帧与调度。

调用方负责（因为这些是与来源相关的）：
  * 建 `Task` 并 commit
  * 调用 `evaluate_task` 评估告警（本函数刻意不碰，避免被调用方重复评估）
"""
from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Detection as DetectionRow
from ..models import Task, TaskStatus, utcnow
from .alerts import evaluate_task
from .imaging import count_by_class, draw_detections, save_image
from .storage import task_dir, url_for_output
from .taskmeta import detection_to_row_kwargs, dump_counts

__all__ = ["persist_frame_task"]


def persist_frame_task(
    db: Session,
    settings: Settings,
    task: Task,
    frame: np.ndarray,
    detections: list[Any],
    inference_ms: float,
    *,
    concurrency: int = 1,
) -> dict[str, int]:
    """把一帧的推理结果写成完整任务，返回分类计数。

    `concurrency` 仅用于日志口径预留，当前恒为 1：单帧图像内每个框都是独立个体，
    不存在可用来去重的跨帧信息。
    """
    directory = task_dir(settings, task.id)
    save_image(directory / "original.jpg", frame)
    save_image(directory / "annotated.jpg", draw_detections(frame, detections))

    for det in detections:
        db.add(DetectionRow(**detection_to_row_kwargs(det, task.id, 1)))

    counts = count_by_class(detections)
    task.status = TaskStatus.DONE
    task.frame_count = 1
    task.total_objects = len(detections)
    task.unique_objects = len(detections)
    task.counts_json = dump_counts(counts)
    task.duration_ms = int(inference_ms)
    task.result_image_path = url_for_output(task.id, "annotated.jpg")
    task.finished_at = utcnow()
    db.commit()

    _ = concurrency  # 保留参数以免将来加多帧来源时改签名
    return counts
