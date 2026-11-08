# -*- coding: utf-8 -*-
"""跟踪器单元测试（纯逻辑，不加载模型，快速）。

核心断言：**同一只虫在多帧里只计一次**，这才是「虫数」的正确口径。
"""
from __future__ import annotations

import numpy as np

from app.services.backends.base import Detection
from app.services.tracker import IouTracker, iou_matrix


def det(x1: int, y1: int, x2: int, y2: int, cls: int = 0, score: float = 0.9) -> Detection:
    return Detection(class_id=cls, class_name=f"class{cls}", score=score, bbox=(x1, y1, x2, y2))


# ---------------------------------------------------------------- IoU
def test_iou_matrix_identical_boxes_is_one():
    a = np.array([[0, 0, 10, 10]], dtype=np.float32)
    assert abs(float(iou_matrix(a, a)[0, 0]) - 1.0) < 1e-5


def test_iou_matrix_disjoint_boxes_is_zero():
    a = np.array([[0, 0, 10, 10]], dtype=np.float32)
    b = np.array([[20, 20, 30, 30]], dtype=np.float32)
    assert float(iou_matrix(a, b)[0, 0]) == 0.0


def test_iou_matrix_handles_empty():
    a = np.zeros((0, 4), dtype=np.float32)
    b = np.array([[0, 0, 10, 10]], dtype=np.float32)
    assert iou_matrix(a, b).shape == (0, 1)


# ---------------------------------------------------------------- 去重计数
def test_same_box_across_frames_counts_once():
    tracker = IouTracker(iou_thresh=0.3, max_age=5)
    detections = [det(0, 0, 10, 10)]
    for _ in range(5):
        stats = tracker.update(detections)

    assert stats.unique_objects == 1
    assert detections[0].track_id == 1


def test_two_separate_objects_get_two_tracks():
    tracker = IouTracker(iou_thresh=0.3)
    detections = [det(0, 0, 10, 10), det(100, 100, 110, 110)]
    stats = tracker.update(detections)

    assert stats.unique_objects == 2
    assert len({d.track_id for d in detections}) == 2


def test_slowly_moving_object_keeps_same_track():
    """虫子会移动，框只重叠一部分时也不应被当成新目标。"""
    tracker = IouTracker(iou_thresh=0.3, max_age=5)
    stats = None
    for offset in range(0, 5):
        stats = tracker.update([det(offset * 2, 0, 10 + offset * 2, 10)])

    assert stats is not None
    assert stats.unique_objects == 1


def test_recycled_track_gets_new_id_after_max_age():
    tracker = IouTracker(iou_thresh=0.3, max_age=2)
    tracker.update([det(0, 0, 10, 10)])   # frame 1：建立轨迹
    tracker.update([])                    # frame 2：misses=1
    tracker.update([])                    # frame 3：misses=2，仍在容忍内
    assert len(tracker.tracks) == 1

    tracker.update([])                    # frame 4：misses=3，回收
    assert len(tracker.tracks) == 0

    tracker.update([det(0, 0, 10, 10)])   # 重新出现 -> 新 id
    assert tracker.unique_count == 2


def test_different_class_is_never_matched():
    """不同类别即使框完全重合，也不能接续同一轨迹。"""
    tracker = IouTracker(iou_thresh=0.3)
    tracker.update([det(0, 0, 10, 10, cls=0)])
    stats = tracker.update([det(0, 0, 10, 10, cls=1)])

    assert stats.unique_objects == 2
    assert stats.by_class.get(0) == 1
    assert stats.by_class.get(1) == 1


def test_min_hits_suppresses_single_frame_false_positive():
    tracker = IouTracker(iou_thresh=0.3, min_hits=2)
    tracker.update([det(0, 0, 10, 10)])
    assert tracker.unique_count == 0     # 只出现一帧，尚不确认

    tracker.update([det(0, 0, 10, 10)])
    assert tracker.unique_count == 1     # 连续两帧，确认


def test_reset_clears_all_state():
    tracker = IouTracker()
    tracker.update([det(0, 0, 10, 10)])
    tracker.reset()

    assert tracker.unique_count == 0
    assert tracker.tracks == []
    assert tracker.frame_index == 0
    assert tracker.confirmed_by_class() == {}
