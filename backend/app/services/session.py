# -*- coding: utf-8 -*-
"""检测会话：把「一个视频/一次摄像头流」的累计状态收拢到一个对象里。

这是对原桌面端的 WebSocket 服务 最关键的修正 —— 旧实现用模块级全局变量
（`current_task` / `stop_flag`）保存进度，第二个客户端一连入就会
打断第一个的任务。现在每个连接持有自己的 `DetectionSession`，
跟踪器、参数、计数器全部互相隔离。
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np

from .detector import DetectorService
from .tracker import IouTracker


class DetectionSession:
    """一次流式检测的累计状态。非线程安全：由所属连接串行调用。"""

    def __init__(
        self,
        session_id: str,
        mode: str,
        detector: DetectorService,
        conf: float | None = None,
        iou: float | None = None,
        tracking: bool = True,
        iou_thresh: float = 0.3,
        max_age: int = 30,
    ) -> None:
        self.session_id = session_id
        self.mode = mode
        self.detector = detector
        self.conf = float(detector.conf if conf is None else conf)
        self.iou = float(detector.iou if iou is None else iou)
        self.tracking = bool(tracking)

        self.tracker = IouTracker(iou_thresh=iou_thresh, max_age=max_age)
        self.frame_index = 0
        self.total_objects = 0                      # 累计框数（逐帧求和）
        self.unique_objects = 0                     # 去重后虫数
        self.class_name_by_id: dict[int, str] = {}
        self.class_total: dict[str, int] = {}       # 逐帧累计的分类框数
        self.started_at = time.time()
        self.last_inference_ms = 0.0

    # ---------------------------------------------------------------- 参数
    def set_params(self, conf: float | None = None, iou: float | None = None) -> None:
        if conf is not None:
            self.conf = min(max(float(conf), 0.0), 1.0)
        if iou is not None:
            self.iou = min(max(float(iou), 0.0), 1.0)

    def reset(self) -> None:
        self.tracker.reset()
        self.frame_index = 0
        self.total_objects = 0
        self.unique_objects = 0
        self.class_total.clear()
        self.class_name_by_id.clear()
        self.started_at = time.time()

    # ---------------------------------------------------------------- 主流程
    def process(
        self,
        bgr: np.ndarray,
        conf: float | None = None,
        iou: float | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """跑一帧，返回 (本帧检测框字典列表, 统计信息)。

        conf/iou 若传入则使用传入值（并同步为本会话的当前值），
        保证「拖动滑块 → 下一帧生效」。
        """
        if conf is not None or iou is not None:
            self.set_params(conf, iou)

        result = self.detector.infer(bgr, self.conf, self.iou)
        detections = result.detections
        self.last_inference_ms = result.inference_ms

        for det in detections:
            self.class_name_by_id.setdefault(det.class_id, det.zh_name or det.class_name)

        if self.tracking:
            stats = self.tracker.update(detections)
            self.unique_objects = stats.unique_objects
            unique_by_class = {
                self.class_name_by_id.get(cid, str(cid)): n
                for cid, n in stats.by_class.items()
            }
        else:
            # 关闭跟踪时退化为「逐帧出现过的类别」，并明确告知调用方 unique 不可用
            self.tracker.frame_index += 1
            unique_by_class = {}

        self.frame_index += 1
        self.total_objects += len(detections)

        # 逐帧累计的分类框数（用于「本帧/累计」两种口径展示）
        frame_by_class: dict[str, int] = {}
        for det in detections:
            name = det.zh_name or det.class_name
            frame_by_class[name] = frame_by_class.get(name, 0) + 1
            self.class_total[name] = self.class_total.get(name, 0) + 1

        payload = [det.to_dict() for det in detections]
        summary = {
            "seq": self.frame_index,
            "frame_total": len(detections),
            "frame_by_class": frame_by_class,
            "total_objects": self.total_objects,
            "unique_objects": self.unique_objects if self.tracking else None,
            "unique_by_class": unique_by_class,
            "active_tracks": len(self.tracker.tracks),
            "inference_ms": round(self.last_inference_ms, 2),
            "elapsed_s": round(time.time() - self.started_at, 1),
        }
        return payload, summary
