# -*- coding: utf-8 -*-
"""推理后端的公共契约。

**为什么要有这一层**：PC 上用 PyTorch+CUDA，田间边缘盒子上要用
ONNX Runtime / NCNN / RKNN / Hailo。若业务代码直接依赖 ultralytics，
换平台就得改遍所有调用点。抽象出 `InferenceBackend` 后，
换后端只改 `config.yaml` 的一行。

同时刻意**不依赖任何 GUI 框架** —— Web 后端进程不应拖入 GUI 依赖。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Detection:
    """单个检测框。

    注意字段顺序：有默认值的字段必须排在后面。
    后端只负责填「模型的原始类别名」，`zh_name` 由 detector 依据
    label_map.json 补全 —— 类别命名是业务概念，不该散落在各后端里。
    """

    class_id: int
    class_name: str                  # 模型原始名（英文）
    score: float
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    zh_name: str = ""                # 中文名（来自 label_map.json）
    track_id: int | None = None      # 由 tracker 填充

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["bbox"] = list(self.bbox)
        return data


@dataclass
class InferenceResult:
    """一帧的推理输出。"""

    detections: list[Detection] = field(default_factory=list)
    inference_ms: float = 0.0

    @property
    def count(self) -> int:
        return len(self.detections)


class BackendUnavailableError(RuntimeError):
    """后端依赖缺失或模型无法加载时抛出，由 detector 决定回退策略。"""


@runtime_checkable
class InferenceBackend(Protocol):
    """所有推理后端的接口。"""

    name: str

    def load(self, model_path: str, device: str, imgsz: int) -> None:
        """加载权重。device 形如 'cuda:0' / 'cpu'。"""

    def infer(
        self,
        bgr: Any,
        conf: float,
        iou: float,
        max_det: int = 300,
    ) -> InferenceResult:
        """对单帧 BGR 图推理。返回带耗时的结果（耗时随结果一起传递，避免额外的出参）。"""

    def warmup(self) -> None:
        """预热，避免首帧耗时异常。"""

    def info(self) -> dict[str, Any]:
        """返回 {name, device, gpu_available, loaded, model_path, num_classes}。"""
