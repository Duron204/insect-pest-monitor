# -*- coding: utf-8 -*-
"""ONNX Runtime 推理后端（阶段 C：树莓派 5 等边缘设备）。

**为什么需要它**：田间边缘盒子上装 PyTorch 不现实 —— wheel 体积大，
ARM 上装 CUDA 版几乎不可行。ONNX Runtime 在 aarch64 有官方 wheel，
树莓派 5 的四核 A76 跑 yolov8n 足够（10fps 量级）。

**这里唯一的难点是与 ultralytics 的数值对齐**：预处理（letterbox）或后处理
（解码 / NMS）任何一个细节不一致，检出框都会与 torch 端对不上，而且症状是
"能跑但结果偏一点"，很难察觉。因此下面每条都刻意与 ultralytics 对齐：

- letterbox：保持长宽比、灰度 (114,114,114) 居中补边到 imgsz×imgsz，
  默认**允许放大**（scaleup=True），插值 INTER_LINEAR
  —— 对应 `LetterBox(imgsz, auto=False, scaleup=True)`
- YOLOv8 的 ONNX 输出**已是 sigmoid 之后的类别概率**，不能再做激活
- 框是输入图坐标系下的 xywh **像素值**（未归一化）
- 类别通道在 box 通道之后：输出形如 (1, 4+nc, 8400)
- NMS 按类别分别做（agnostic=False），与 ultralytics 默认一致

对齐结论由 `tests/test_onnx_backend.py` 与 torch 后端实测比对得出，
不靠"看起来对"。

注意：onnxruntime 采用**惰性导入** —— 主环境（跑 torch）不装它也能 import 本模块，
只是实例化时抛 BackendUnavailableError，由 detector 决定是否回退。
"""
from __future__ import annotations

import ast
import os
import time
from typing import Any

import cv2
import numpy as np

from .base import BackendUnavailableError, Detection, InferenceResult

# letterbox 补边灰度：与 ultralytics LetterBox 的 (114, 114, 114) 保持一致
_PAD_VALUE = (114, 114, 114)
# 环境变量可覆盖线程数（0/未设 = 交给 ONNX Runtime 自行决定）
_ENV_THREADS = "INSECT_ONNX_THREADS"


def letterbox(
    bgr: np.ndarray, imgsz: int, scaleup: bool = True
) -> tuple[np.ndarray, float, float, float]:
    """等比缩放 + 灰度补边到 (imgsz, imgsz)。

    返回 (补边后的图, 缩放比, 左侧补边, 上方补边)，供坐标还原使用。
    与 ultralytics `LetterBox(auto=False)` 对齐：补边居中，dw/dh 各分一半。
    """
    height, width = bgr.shape[:2]
    ratio = min(imgsz / height, imgsz / width)
    if not scaleup:
        ratio = min(ratio, 1.0)

    new_w, new_h = int(round(width * ratio)), int(round(height * ratio))
    if (width, height) != (new_w, new_h):
        bgr = cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    dw = (imgsz - new_w) / 2
    dh = (imgsz - new_h) / 2
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    padded = cv2.copyMakeBorder(
        bgr, top, bottom, left, right, cv2.BORDER_CONSTANT, value=_PAD_VALUE
    )
    return padded, ratio, float(left), float(top)


def nms_indices(
    boxes_xyxy: np.ndarray, scores: np.ndarray, iou_thres: float, max_det: int
) -> list[int]:
    """贪心 NMS（与 torchvision.ops.nms 同一套逻辑）。返回保留的下标。"""
    if boxes_xyxy.size == 0:
        return []
    x1, y1, x2, y2 = (
        boxes_xyxy[:, 0],
        boxes_xyxy[:, 1],
        boxes_xyxy[:, 2],
        boxes_xyxy[:, 3],
    )
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0 and len(keep) < max_det:
        current = int(order[0])
        keep.append(current)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[current], x1[rest])
        yy1 = np.maximum(y1[current], y1[rest])
        xx2 = np.minimum(x2[current], x2[rest])
        yy2 = np.minimum(y2[current], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[current] + areas[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[iou <= iou_thres]
    return keep


def _read_class_names(session: Any) -> dict[int, str]:
    """从 ONNX 元数据读取类别名（ultralytics 导出时会写入 `names`）。"""
    try:
        meta = session.get_modelmeta().custom_metadata_map or {}
    except Exception:  # noqa: BLE001 - 元数据缺失不应导致整个后端不可用
        return {}
    raw = meta.get("names")
    if not raw:
        return {}
    # ultralytics 写入的是 Python dict 的字面量文本
    parsed: Any
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {}
    if isinstance(parsed, dict):
        return {int(k): str(v) for k, v in parsed.items()}
    if isinstance(parsed, (list, tuple)):
        return {i: str(v) for i, v in enumerate(parsed)}
    return {}


class OnnxBackend:
    """ONNX Runtime 后端。构造无参，与 `DetectorService._create_backend` 的约定一致。"""

    name = "onnx"

    def __init__(self) -> None:
        self._session: Any = None
        self._input_name: str = ""
        self._loaded = False
        self._model_path: str = ""
        self._imgsz: int = 640
        self._device: str = "cpu"
        self._providers: list[str] = []
        self._names: dict[int, str] = {}
        self._num_classes: int = 0
        self._scaleup = True

    # ---------------------------------------------------------------- 加载
    def load(self, model_path: str, device: str = "cpu", imgsz: int = 640) -> None:
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(
                f"未安装 onnxruntime（pip install onnxruntime）：{exc}"
            ) from exc

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        threads = int(os.environ.get(_ENV_THREADS, "0") or 0)
        if threads > 0:
            options.intra_op_num_threads = threads

        providers = self._pick_providers(ort, device)
        try:
            session = ort.InferenceSession(
                str(model_path), sess_options=options, providers=providers
            )
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(f"ONNX 模型加载失败（{model_path}）：{exc}") from exc

        self._session = session
        self._input_name = session.get_inputs()[0].name
        inputs = session.get_inputs()[0]
        # 输入形状为 (1, 3, H, W)；动态维（字符串）时退回配置的 imgsz
        shape = list(inputs.shape)
        if len(shape) == 4 and isinstance(shape[2], int) and shape[2] > 0:
            self._imgsz = int(shape[2])
        else:
            self._imgsz = int(imgsz)

        self._names = _read_class_names(session)
        self._num_classes = len(self._names)
        self._model_path = str(model_path)
        self._providers = list(session.get_providers())
        self._device = "cuda" if any("CUDA" in p for p in self._providers) else "cpu"
        self._loaded = True

    @staticmethod
    def _pick_providers(ort: Any, device: str) -> list[str]:
        """按请求的设备挑选 ExecutionProvider，不可用时退回 CPU。"""
        available = set(ort.get_available_providers())
        wanted = (device or "cpu").lower()
        if wanted.startswith("cuda") and "CUDAExecutionProvider" in available:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]

    # ---------------------------------------------------------------- 推理
    def infer(
        self,
        bgr: np.ndarray,
        conf: float,
        iou: float,
        max_det: int = 300,
    ) -> InferenceResult:
        if self._session is None:
            raise BackendUnavailableError("ONNX 模型尚未加载")

        started = time.perf_counter()
        padded, ratio, pad_x, pad_y = letterbox(bgr, self._imgsz, self._scaleup)
        rgb = padded[:, :, ::-1]                      # BGR -> RGB
        blob = rgb.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        blob = np.ascontiguousarray(blob)

        outputs = self._session.run(None, {self._input_name: blob})
        detections = self._decode(outputs[0], bgr.shape, ratio, pad_x, pad_y, conf, iou, max_det)
        elapsed = (time.perf_counter() - started) * 1000
        return InferenceResult(detections=detections, inference_ms=elapsed)

    def _decode(
        self,
        raw: np.ndarray,
        original_shape: tuple[int, ...],
        ratio: float,
        pad_x: float,
        pad_y: float,
        conf: float,
        iou: float,
        max_det: int,
    ) -> list[Detection]:
        pred = raw[0] if raw.ndim == 3 else raw

        # 兼容"已内嵌 NMS"的导出：形如 (N, 6) = x1,y1,x2,y2,score,class
        if pred.ndim == 2 and pred.shape[1] == 6 and pred.shape[0] <= 1000:
            return self._decode_nms_embedded(pred, original_shape, ratio, pad_x, pad_y, conf, max_det)

        # YOLOv8 原始输出为 (4+nc, anchors)，转成 (anchors, 4+nc)
        if pred.shape[0] < pred.shape[1]:
            pred = pred.T
        if pred.ndim != 2 or pred.shape[1] <= 4:
            return []

        boxes_xywh = pred[:, :4]
        class_scores = pred[:, 4:]
        class_ids = class_scores.argmax(axis=1)
        scores = class_scores[np.arange(class_ids.shape[0]), class_ids]

        mask = scores >= conf
        if not mask.any():
            return []
        boxes_xywh = boxes_xywh[mask]
        scores = scores[mask]
        class_ids = class_ids[mask]

        # xywh -> xyxy（此时仍是 letterbox 输入图坐标系）
        xyxy = np.empty_like(boxes_xywh)
        xyxy[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2
        xyxy[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2
        xyxy[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2
        xyxy[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2

        # 去掉补边并还原缩放，回到原图坐标
        xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_x) / ratio
        xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_y) / ratio
        height, width = original_shape[:2]
        xyxy[:, [0, 2]] = xyxy[:, [0, 2]].clip(0, width)
        xyxy[:, [1, 3]] = xyxy[:, [1, 3]].clip(0, height)

        return self._build(xyxy, scores, class_ids, iou, max_det)

    def _decode_nms_embedded(
        self,
        pred: np.ndarray,
        original_shape: tuple[int, ...],
        ratio: float,
        pad_x: float,
        pad_y: float,
        conf: float,
        max_det: int,
    ) -> list[Detection]:
        mask = pred[:, 4] >= conf
        if not mask.any():
            return []
        rows = pred[mask]
        xyxy = rows[:, :4].copy()
        xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_x) / ratio
        xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_y) / ratio
        height, width = original_shape[:2]
        xyxy[:, [0, 2]] = xyxy[:, [0, 2]].clip(0, width)
        xyxy[:, [1, 3]] = xyxy[:, [1, 3]].clip(0, height)
        return self._build(xyxy, rows[:, 4].astype(np.float64), rows[:, 5].astype(np.int64), 1.0, max_det)

    def _build(
        self,
        xyxy: np.ndarray,
        scores: np.ndarray,
        class_ids: np.ndarray,
        iou: float,
        max_det: int,
    ) -> list[Detection]:
        # 按类别分别 NMS（agnostic=False），与 ultralytics 默认一致
        kept: list[int] = []
        for class_id in np.unique(class_ids):
            group = np.flatnonzero(class_ids == class_id)
            kept.extend(group[index] for index in nms_indices(xyxy[group], scores[group], iou, max_det))
        kept.sort(key=lambda index: -float(scores[index]))
        kept = kept[:max_det]

        detections: list[Detection] = []
        for index in kept:
            x1, y1, x2, y2 = (int(round(float(value))) for value in xyxy[index])
            if x2 <= x1 or y2 <= y1:
                continue
            class_id = int(class_ids[index])
            detections.append(
                Detection(
                    class_id=class_id,
                    class_name=self._names.get(class_id, str(class_id)),
                    score=float(scores[index]),
                    bbox=(x1, y1, x2, y2),
                )
            )
        return detections

    def warmup(self) -> None:
        if self._session is None:
            return
        blank = np.zeros((self._imgsz, self._imgsz, 3), dtype=np.uint8)
        try:
            self.infer(blank, conf=0.5, iou=0.5, max_det=1)
        except Exception:  # noqa: BLE001 - 预热失败不应影响启动
            pass

    def compute_heatmap(self, bgr: np.ndarray) -> np.ndarray | None:
        """ONNX 后端不提供热力图（需要中间层输出，属 torch 专属能力）。

        返回 None 而不是抛异常：detector.heatmap() 会直接调用本方法，
        未实现时应表现为"没有热力图"，而不是把接口打成 500。
        """
        return None

    # ---------------------------------------------------------------- 信息
    @property
    def names(self) -> dict[int, str]:
        return dict(self._names)

    @property
    def loaded(self) -> bool:
        return self._loaded

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "device": self._device,
            "gpu_available": self._device != "cpu",
            "loaded": self._loaded,
            "model_path": self._model_path,
            "num_classes": self._num_classes,
            "providers": list(self._providers),
            "imgsz": self._imgsz,
            "heatmap": False,
        }
