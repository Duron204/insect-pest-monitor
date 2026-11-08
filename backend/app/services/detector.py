# -*- coding: utf-8 -*-
"""推理服务编排层。

职责：
- 按 `config.yaml` 选择并持有推理后端（换平台只改一行配置）
- 持有当前 conf/iou 参数，并**显式传给后端**（不再依赖共享可变 dict 的巧合）
- 用 `label_map.json` 补全中文类别名（类别命名是业务概念，不散落到各后端）
- 提供线程安全的推理入口

关于线程：推理是阻塞的（CPU/GPU 都在等），调用方应放到线程池执行，
这里只用一把 `RLock` 串行化设备访问 —— 本机单实例下串行即最优，
也避免多请求并发抢 GPU 导致显存抖动。
"""
from __future__ import annotations

import json
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Settings
from .backends.base import BackendUnavailableError, InferenceResult

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


# ---------------------------------------------------------------- 类别映射
@lru_cache(maxsize=1)
def _label_data() -> tuple[dict[int, str], dict[str, str]]:
    """返回 (按 class_id 索引的中文名, 按英文名小写索引的中文名)。"""
    by_id: dict[int, str] = {}
    by_en: dict[str, str] = {}
    path = _DATA_DIR / "label_map.json"
    if not path.exists():
        return by_id, by_en
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return by_id, by_en
    for item in raw.get("classes", []):
        try:
            class_id = int(item["class_id"])
        except (KeyError, TypeError, ValueError):
            continue
        en = str(item.get("en", "")).strip()
        zh = str(item.get("zh") or en).strip()
        by_id[class_id] = zh
        if en:
            by_en[en.lower()] = zh
    return by_id, by_en


def zh_name_for(class_id: int, class_name: str) -> str:
    """给定类别 id/英文名，返回中文名；查不到则原样返回（不隐藏未知类别）。"""
    by_id, by_en = _label_data()
    if class_id in by_id:
        return by_id[class_id]
    return by_en.get((class_name or "").strip().lower(), class_name or "")


def known_class_count() -> int:
    by_id, _ = _label_data()
    return len(by_id)


# ---------------------------------------------------------------- 服务
class DetectorService:
    """进程内共享的推理服务。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._backend: Any = None
        self._backend_name: str = settings.inference.backend
        self._model_path: str | None = None
        self._conf: float = float(settings.inference.conf)
        self._iou: float = float(settings.inference.iou)
        self._fallback_reason: str | None = None

    # ---------------------------------------------------------------- 模型
    def _create_backend(self, name: str) -> Any:
        """按名称创建后端。未实现/缺依赖时回退 TorchBackend 并记录原因。

        注意：这里**必须同时检查运行期依赖**。onnx_backend 把
        `import onnxruntime` 放在 load() 里（这样主环境不装它也能 import 本模块），
        若这里只 import 类本身，缺依赖就要等到加载权重时才炸 ——
        而 load() 是硬失败、不回退，于是"缺失时自动回退 torch"这句话就成了空话。
        """
        wanted = (name or "torch").strip().lower()

        if wanted == "onnx":
            try:
                import onnxruntime  # noqa: F401 - 仅用于确认依赖存在

                from .backends.onnx_backend import OnnxBackend

                return OnnxBackend()
            except Exception as exc:  # noqa: BLE001
                self._fallback_reason = f"ONNX 后端不可用（{exc}），已回退 torch"
        elif wanted in ("ncnn", "rknn", "hailo"):
            self._fallback_reason = f"后端 {wanted} 将在阶段C实现，已回退 torch"

        from .backends.torch_backend import TorchBackend

        return TorchBackend(heatmap=bool(self._settings.inference.heatmap))

    def load(self, model_path: str | None = None) -> None:
        """加载（或切换）权重。路径可为相对项目根的相对路径。"""
        raw = model_path or self._settings.inference.default_model
        path = Path(raw)
        if not path.is_absolute():
            path = (self._settings.project_root / path).resolve()
        if not path.exists():
            raise BackendUnavailableError(f"权重文件不存在：{path}")

        with self._lock:
            self._fallback_reason = None
            backend = self._create_backend(self._settings.inference.backend)
            backend.load(
                str(path),
                device=self._settings.inference.device,
                imgsz=self._settings.inference.imgsz,
            )
            # 加载成功后才替换，避免失败时把旧模型也弄丢
            self._backend = backend
            self._backend_name = backend.name
            self._model_path = str(path)

    def ensure_loaded(self) -> None:
        if self._backend is None:
            self.load()

    @property
    def loaded(self) -> bool:
        return self._backend is not None

    # ---------------------------------------------------------------- 参数
    @property
    def conf(self) -> float:
        return self._conf

    @property
    def iou(self) -> float:
        return self._iou

    def set_params(self, conf: float | None = None, iou: float | None = None) -> None:
        """更新阈值。这是滑块生效的**唯一**入口，下一帧立即生效。"""
        with self._lock:
            if conf is not None:
                self._conf = min(max(float(conf), 0.0), 1.0)
            if iou is not None:
                self._iou = min(max(float(iou), 0.0), 1.0)

    def params(self) -> dict[str, float]:
        return {"conf": self._conf, "iou": self._iou}

    # ---------------------------------------------------------------- 推理
    def infer(
        self,
        bgr: np.ndarray,
        conf: float | None = None,
        iou: float | None = None,
    ) -> InferenceResult:
        """线程安全的单帧推理；conf/iou 显式传入，不读共享状态。"""
        self.ensure_loaded()
        use_conf = self._conf if conf is None else float(conf)
        use_iou = self._iou if iou is None else float(iou)

        with self._lock:
            result: InferenceResult = self._backend.infer(
                bgr,
                use_conf,
                use_iou,
                max_det=int(self._settings.inference.max_det),
            )

        for det in result.detections:
            det.zh_name = zh_name_for(det.class_id, det.class_name)
        return result

    def heatmap(self, bgr: np.ndarray) -> np.ndarray | None:
        with self._lock:
            if self._backend is None:
                return None
            return self._backend.compute_heatmap(bgr)

    # ---------------------------------------------------------------- 信息
    def names(self) -> dict[int, str]:
        if self._backend is None:
            return {}
        return dict(getattr(self._backend, "names", {}) or {})

    def info(self) -> dict[str, Any]:
        if self._backend is None:
            return {
                "name": self._backend_name,
                "device": "unloaded",
                "gpu_available": False,
                "loaded": False,
                "model_path": None,
                "num_classes": known_class_count(),
                "conf": self._conf,
                "iou": self._iou,
            }
        data = dict(self._backend.info())
        data["conf"] = self._conf
        data["iou"] = self._iou
        if self._fallback_reason:
            data["fallback_reason"] = self._fallback_reason
        return data
