# -*- coding: utf-8 -*-
"""任务元数据的小工具：分类计数的序列化与进度计算。"""
from __future__ import annotations

import json
from typing import Any

from ..models import Task, TaskStatus


def dump_counts(counts: dict[str, int]) -> str:
    return json.dumps(counts, ensure_ascii=False, sort_keys=True)


def load_counts(task: Task | None) -> dict[str, int]:
    """读取任务的分类计数；数据损坏时返回空字典而不是抛异常。"""
    if task is None or not task.counts_json:
        return {}
    try:
        data = json.loads(task.counts_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in data.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def merge_counts(target: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    for name, count in extra.items():
        target[name] = target.get(name, 0) + int(count)
    return target


def task_progress(task: Task | None) -> float:
    """0~100 的进度。没有总帧数时，未完成任务返回 0（而不是假装有进度）。"""
    if task is None:
        return 0.0
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
        return 100.0
    return video_progress(task.frame_count, task.total_frames or 0)


def video_progress(frame_count: int, total_frames: int) -> float:
    if total_frames <= 0:
        return 0.0
    return min(100.0, round(frame_count / float(total_frames) * 100.0, 1))


def detection_to_row_kwargs(det: Any, task_id: int, frame_index: int) -> dict[str, Any]:
    """把 Detection 转成 DetectionRow 的构造参数（集中一处便于字段演进）。"""
    return {
        "task_id": task_id,
        "frame_index": frame_index,
        "class_id": int(det.class_id),
        "class_name": det.class_name,
        "score": float(det.score),
        "x1": int(det.bbox[0]),
        "y1": int(det.bbox[1]),
        "x2": int(det.bbox[2]),
        "y2": int(det.bbox[3]),
        "track_id": det.track_id,
    }
