# -*- coding: utf-8 -*-
"""PyTorch / ultralytics 推理后端（PC 与 Jetson 用）。

相对于原桌面端的检测封装，这里修掉三处缺陷：

1. **热力图 hook 泄漏**：旧实现每次 `load_model` 都 `register_hook` 且从不移除，
   反复切换模型会累积 hook（内存泄漏 + 热力图错乱）。这里统一 `_detach_hooks()`。
2. **取层脆弱**：旧实现写死 `list(model.model.children())[0][-3]`，
   只对某一种 YOLOv8 结构成立，换 YOLOv11/YOLOv5 就会取错层甚至 IndexError。
   这里改为「按模块类型优先级自适应挑选 + 前向探针验证」，验证失败就自动关闭热力图。
3. **参数靠巧合生效**：旧实现把 `self.params` 指向模块级 `ini_params` 同一个 dict，
   而 `predict()` 用的是 `ini_params` —— 外部一旦传入自定义参数就静默失效。
   这里 conf/iou 一律由调用方显式传入，不依赖任何共享可变状态。

同时**不依赖任何 GUI 框架**：Web 后端进程不应拖入 GUI 依赖，
热力图也自己实现（复用推理时捕获的激活，不额外做一次前向）。
"""
from __future__ import annotations

import time
from typing import Any

import cv2
import numpy as np

from .base import BackendUnavailableError, Detection, InferenceResult

# 按优先级从高到低挑选热力图观测层。
# 这些是 YOLOv5/v8/v11 主干末端的典型模块名，越靠前越接近语义高层。
_HEATMAP_LAYER_PRIORITY = ("SPPF", "C2PSA", "C2f", "C3k2", "C3", "BottleneckCSP", "Conv")


def resolve_torch_device(preference: str = "auto") -> str:
    """把配置里的 auto/cuda:0/cpu 解析成 ultralytics 可用的设备字符串。"""
    try:
        import torch
    except ImportError as exc:  # pragma: no cover
        raise BackendUnavailableError("未安装 torch") from exc

    pref = (preference or "auto").strip().lower()
    if pref in ("cpu",):
        return "cpu"
    if pref.startswith("cuda"):
        if not torch.cuda.is_available():
            # 明确要求 GPU 但不可用：退回 CPU，而不是直接崩
            return "cpu"
        return "cuda:0" if pref == "cuda" else pref
    return "cuda:0" if torch.cuda.is_available() else "cpu"


def _iter_modules(model: Any):
    """取出 nn.Sequential 形式的层列表。"""
    inner = getattr(model, "model", None)
    if inner is None:
        return []
    seq = getattr(inner, "model", inner)
    try:
        return list(seq)
    except TypeError:
        return []


def _select_heatmap_layer(model: Any):
    """自适应挑选热力图观测层；找不到返回 None（调用方据此关闭热力图）。"""
    modules = _iter_modules(model)
    if not modules:
        return None
    for wanted in _HEATMAP_LAYER_PRIORITY:
        for module in reversed(modules):
            if type(module).__name__ == wanted:
                return module
    return None


class _ActivationCatcher:
    """前向 hook 的载体：保存最近一次前向的激活张量。"""

    def __init__(self) -> None:
        self.value: Any = None

    def __call__(self, _module, _inputs, output):  # noqa: ANN001
        try:
            self.value = output.detach()
        except AttributeError:
            self.value = None


class TorchBackend:
    """基于 ultralytics YOLO 的推理后端。"""

    name = "torch"

    def __init__(self, heatmap: bool = False) -> None:
        self._model: Any = None
        self._loaded = False
        self._heatmap_requested = bool(heatmap)
        self.heatmap_enabled = False
        self._hooks: list[Any] = []
        self._catcher: _ActivationCatcher | None = None
        self.model_path: str | None = None
        self.device: str = "cpu"
        self.imgsz: int = 640
        self.names: dict[int, str] = {}

    # ---------------------------------------------------------------- 加载
    def load(self, model_path: str, device: str = "auto", imgsz: int = 640) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise BackendUnavailableError("未安装 ultralytics，无法使用 TorchBackend") from exc

        # 关键：先清掉旧 hook，再换模型，否则反复切换会持续泄漏
        self._detach_hooks()

        self.device = resolve_torch_device(device)
        self.imgsz = int(imgsz)
        try:
            self._model = YOLO(model_path)
        except Exception as exc:
            raise BackendUnavailableError(f"加载权重失败：{model_path} ({exc})") from exc

        raw_names = getattr(self._model, "names", None) or {}
        self.names = {int(k): str(v) for k, v in dict(raw_names).items()}
        self.model_path = model_path
        self._loaded = True

        self.warmup()
        if self._heatmap_requested:
            self._setup_heatmap()

    def warmup(self) -> None:
        """用一张空图跑一次完整前向，避免首帧耗时异常。"""
        if self._model is None:
            return
        blank = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
        self._model.predict(
            source=blank,
            imgsz=self.imgsz,
            device=self.device,
            verbose=False,
        )

    def _detach_hooks(self) -> None:
        for handle in self._hooks:
            try:
                handle.remove()
            except Exception:  # noqa: BLE001 - 移除失败不应影响主流程
                pass
        self._hooks.clear()
        self._catcher = None
        self.heatmap_enabled = False

    def _setup_heatmap(self) -> None:
        """挂 hook 并用一次前向验证真的能拿到 4D 激活；失败则安静关闭。"""
        self._detach_hooks()
        layer = _select_heatmap_layer(self._model)
        if layer is None:
            return

        catcher = _ActivationCatcher()
        try:
            self._hooks.append(layer.register_forward_hook(catcher))
        except Exception:  # noqa: BLE001
            self._hooks.clear()
            return
        self._catcher = catcher

        try:
            probe = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
            self._model.predict(
                source=probe, imgsz=self.imgsz, device=self.device, verbose=False
            )
        except Exception:  # noqa: BLE001
            self._detach_hooks()
            return

        value = catcher.value
        if value is None or getattr(value, "ndim", 0) != 4:
            # 结构不认识 —— 关闭热力图而不是带着坏 hook 继续跑
            self._detach_hooks()
            return
        self.heatmap_enabled = True

    # ---------------------------------------------------------------- 推理
    def infer(
        self,
        bgr: np.ndarray,
        conf: float,
        iou: float,
        max_det: int = 300,
    ) -> InferenceResult:
        if not self._loaded or self._model is None:
            raise BackendUnavailableError("模型尚未加载")

        started = time.perf_counter()
        results = self._model.predict(
            source=bgr,
            conf=float(conf),
            iou=float(iou),
            imgsz=self.imgsz,
            device=self.device,
            max_det=int(max_det),
            verbose=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        detections: list[Detection] = []
        if results:
            boxes = getattr(results[0], "boxes", None)
            if boxes is not None and len(boxes) > 0:
                xyxy = boxes.xyxy.cpu().numpy()
                classes = boxes.cls.cpu().numpy().astype(int)
                scores = boxes.conf.cpu().numpy()
                for box, class_id, score in zip(xyxy, classes, scores):
                    x1, y1, x2, y2 = (int(v) for v in box[:4])
                    detections.append(
                        Detection(
                            class_id=int(class_id),
                            class_name=self.names.get(int(class_id), str(class_id)),
                            score=float(score),
                            bbox=(x1, y1, x2, y2),
                        )
                    )

        return InferenceResult(detections=detections, inference_ms=elapsed_ms)

    # ---------------------------------------------------------------- 热力图
    def compute_heatmap(self, bgr: np.ndarray) -> np.ndarray | None:
        """BGR 热力图叠加图。复用 infer() 时捕获的激活，不额外前向。"""
        if not self.heatmap_enabled or self._catcher is None:
            return None
        activation = self._catcher.value
        if activation is None or getattr(activation, "ndim", 0) != 4:
            return None

        # 先按通道做 ReLU 再求均值，**不能反序**。
        # 早先是 `maximum(mean(x), 0)`：跨通道平均时正负会互相抵消，
        # 只要某个空间位置的通道均值为负就被抹平。SPPF 输出只有约 15% 的值为正，
        # 于是这个写法能否出图完全取决于模型/输入 —— 实测 YOLOv8n 勉强有正值可用，
        # 换成 YOLO11s 后整幅图均值全负，热力图**静默消失**（返回 None，
        # 调用方只会以为"这次没有热力图"）。
        cam = activation.clamp(min=0).mean(dim=1)[0].cpu().numpy().astype(np.float32)
        peak = float(cam.max())
        if peak <= 0:
            # 真的是全负激活（例如空白输入）—— 如实返回 None，不编造一张图
            return None
        cam /= peak

        height, width = bgr.shape[:2]
        cam = cv2.resize(cam, (width, height), interpolation=cv2.INTER_LINEAR)
        heat = cv2.applyColorMap((cam * 255).astype(np.uint8), cv2.COLORMAP_JET)
        return cv2.addWeighted(bgr, 0.6, heat, 0.4, 0.0)

    # ---------------------------------------------------------------- 信息
    def info(self) -> dict[str, Any]:
        gpu_available = False
        try:
            import torch

            gpu_available = bool(torch.cuda.is_available())
        except ImportError:  # pragma: no cover
            pass
        return {
            "name": self.name,
            "device": self.device,
            "gpu_available": gpu_available,
            "loaded": self._loaded,
            "model_path": self.model_path,
            "num_classes": len(self.names),
            "heatmap": self.heatmap_enabled,
        }
