# -*- coding: utf-8 -*-
"""与推理解耦的多目标跟踪器（IoU 贪心匹配）。

**为什么不用 ultralytics 的 `model.track()`**
1. 只有 TorchBackend 有 track()，ONNX/NCNN/RKNN/Hailo 后端都没有 ——
   而「去重计数」必须在所有后端上表现一致，否则换平台数字就变。
2. `model.track(persist=True)` 把跟踪状态挂在模型对象上，而模型是进程内
   共享单例 —— 多会话、多次任务之间会互相污染，正是原桌面端的 WebSocket 服务
   全局状态问题的翻版。
自己实现后，跟踪状态属于**会话**，与推理后端彻底解耦。

算法：IoU 降序贪心匹配（同类才匹配）+ 轨迹生命周期管理（max_age 回收）。
对虫情计数足够：我们关心「同一只虫不被重复计数」，不追求轨迹平滑度，
因此不引入卡尔曼滤波，保持零额外依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .backends.base import Detection


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a:(N,4), b:(M,4) -> (N,M) 的 IoU 矩阵（全向量化，逐帧调用不拖慢推理）。"""
    if a.size == 0 or b.size == 0:
        return np.zeros((a.shape[0], b.shape[0]), dtype=np.float32)

    ax1, ay1, ax2, ay2 = a[:, 0:1], a[:, 1:2], a[:, 2:3], a[:, 3:4]
    bx1, by1, bx2, by2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]

    inter_w = np.clip(np.minimum(ax2, bx2) - np.maximum(ax1, bx1), 0, None)
    inter_h = np.clip(np.minimum(ay2, by2) - np.maximum(ay1, by1), 0, None)
    inter = inter_w * inter_h

    area_a = np.clip(ax2 - ax1, 0, None) * np.clip(ay2 - ay1, 0, None)
    area_b = np.clip(bx2 - bx1, 0, None) * np.clip(by2 - by1, 0, None)
    union = area_a + area_b - inter

    return np.where(union > 0, inter / np.maximum(union, 1e-6), 0.0).astype(np.float32)


@dataclass
class Track:
    """一条轨迹。bbox 用 float 保存匹配结果，不参与像素取整。"""

    track_id: int
    class_id: int
    bbox: tuple[float, float, float, float]
    score: float
    hits: int = 1
    misses: int = 0
    first_frame: int = 0
    last_frame: int = 0


@dataclass
class TrackStats:
    frame_index: int = 0
    unique_objects: int = 0                # 累计确认的不同目标数（去重后虫数）
    active_tracks: int = 0
    new_tracks: int = 0
    by_class: dict[int, int] = field(default_factory=dict)  # 去重后的分类计数


class IouTracker:
    def __init__(
        self,
        iou_thresh: float = 0.3,
        max_age: int = 30,
        min_hits: int = 1,
    ) -> None:
        self.iou_thresh = float(iou_thresh)
        self.max_age = max(0, int(max_age))
        self.min_hits = max(1, int(min_hits))
        self.tracks: list[Track] = []
        self.frame_index = 0
        self._next_id = 1
        self._confirmed_ids: set[int] = set()
        self._confirmed_by_class: dict[int, set[int]] = {}

    # ---------------------------------------------------------------- 查询
    @property
    def unique_count(self) -> int:
        """去重后的目标总数 —— 这才是「虫数」，而不是逐帧框数求和。"""
        return len(self._confirmed_ids)

    def confirmed_by_class(self) -> dict[int, int]:
        return {cid: len(ids) for cid, ids in self._confirmed_by_class.items() if ids}

    def reset(self) -> None:
        self.tracks.clear()
        self.frame_index = 0
        self._next_id = 1
        self._confirmed_ids.clear()
        self._confirmed_by_class.clear()

    # ---------------------------------------------------------------- 主流程
    def update(self, detections: list[Detection]) -> TrackStats:
        """把 track_id 回填到 detections 上，并返回本帧统计。"""
        self.frame_index += 1

        det_boxes = (
            np.array([d.bbox for d in detections], dtype=np.float32)
            if detections
            else np.zeros((0, 4), dtype=np.float32)
        )
        trk_boxes = (
            np.array([t.bbox for t in self.tracks], dtype=np.float32)
            if self.tracks
            else np.zeros((0, 4), dtype=np.float32)
        )

        matches: list[tuple[int, int]] = []
        used_det: set[int] = set()
        used_trk: set[int] = set()

        if len(detections) and self.tracks:
            iou = iou_matrix(det_boxes, trk_boxes)
            # 只允许同类匹配：不同类别的框即使重叠也不应接续同一轨迹
            det_cls = np.array([d.class_id for d in detections], dtype=np.int32)
            trk_cls = np.array([t.class_id for t in self.tracks], dtype=np.int32)
            iou = np.where(det_cls[:, None] == trk_cls[None, :], iou, -1.0)

            # 降序贪心：argsort(-iou) 中 -iou 升序即 iou 降序
            for flat in np.argsort(-iou, axis=None):
                i, j = divmod(int(flat), iou.shape[1])
                if iou[i, j] < self.iou_thresh:
                    break  # 已降序，后面只会更小
                if i in used_det or j in used_trk:
                    continue
                used_det.add(i)
                used_trk.add(j)
                matches.append((i, j))

        new_tracks = 0

        # 1) 匹配成功：更新既有轨迹
        for i, j in matches:
            det = detections[i]
            trk = self.tracks[j]
            trk.bbox = (float(det.bbox[0]), float(det.bbox[1]), float(det.bbox[2]), float(det.bbox[3]))
            trk.score = det.score
            trk.hits += 1
            trk.misses = 0
            trk.last_frame = self.frame_index
            det.track_id = trk.track_id
            self._confirm(trk)

        # 2) 未匹配的检测：新建轨迹
        for i, det in enumerate(detections):
            if i in used_det:
                continue
            trk = Track(
                track_id=self._next_id,
                class_id=det.class_id,
                bbox=(float(det.bbox[0]), float(det.bbox[1]), float(det.bbox[2]), float(det.bbox[3])),
                score=det.score,
                first_frame=self.frame_index,
                last_frame=self.frame_index,
            )
            self._next_id += 1
            self.tracks.append(trk)
            det.track_id = trk.track_id
            self._confirm(trk)
            new_tracks += 1

        # 3) 老化并回收未被匹配的轨迹。
        #    判据用 last_frame 而不是 used_trk 索引集合 ——
        #    因为本帧新建的轨迹也已 append 进 self.tracks，
        #    若按索引判断会把「刚出生的轨迹」也老化一次，
        #    使 max_age 实际变成 max_age-1（少容忍一帧）。
        survivors: list[Track] = []
        for trk in self.tracks:
            if trk.last_frame == self.frame_index:
                survivors.append(trk)      # 本帧匹配成功或本帧新建，不老化
                continue
            trk.misses += 1
            if trk.misses <= self.max_age:
                survivors.append(trk)
        self.tracks = survivors

        return TrackStats(
            frame_index=self.frame_index,
            unique_objects=self.unique_count,
            active_tracks=len(self.tracks),
            new_tracks=new_tracks,
            by_class=self.confirmed_by_class(),
        )

    def _confirm(self, trk: Track) -> None:
        """达到 min_hits 才计入唯一目标数，用于抑制单帧误检带来的虚高计数。"""
        if trk.hits < self.min_hits:
            return
        self._confirmed_ids.add(trk.track_id)
        self._confirmed_by_class.setdefault(trk.class_id, set()).add(trk.track_id)
