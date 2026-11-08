# -*- coding: utf-8 -*-
"""推理服务测试（会真正加载模型，较慢但价值最高）。

覆盖实施计划里点名的三处旧缺陷：
1. conf/iou 必须真正生效（旧实现依赖共享 dict 的巧合，外部传参即静默失效）；
2. 热力图 hook 不得泄漏（旧实现每次 load_model 都 register_hook 且从不移除）；
3. 类别中文名必须补全，且 bbox 落在图像范围内。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.config import load_settings
from app.services.backends.torch_backend import TorchBackend
from app.services.detector import DetectorService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE = PROJECT_ROOT / "test_media" / "InsectPest_SIXU_A00025.jpg"
DEFAULT_WEIGHTS = PROJECT_ROOT / "weights" / "best-yolo11s.pt"

#: 权重被 .gitignore 排除（18MB 二进制不进仓库），因此 CI 上没有它。
#: 这种情况**显式跳过**并在报告里写清原因，而不是让 CI 无故失败。
pytestmark = pytest.mark.skipif(
    not DEFAULT_WEIGHTS.is_file(), reason=f"缺少权重 {DEFAULT_WEIGHTS.name}（仓库不存二进制）"
)


def _load_image(path: Path) -> np.ndarray:
    """中文路径安全读图：cv2.imread 在非 ASCII 路径上会静默返回 None。"""
    buffer = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    assert image is not None, f"无法读取测试图片：{path}"
    return image


@pytest.fixture(scope="module")
def image() -> np.ndarray:
    return _load_image(TEST_IMAGE)


@pytest.fixture(scope="module")
def detector() -> DetectorService:
    service = DetectorService(load_settings())
    service.load(str(DEFAULT_WEIGHTS))
    return service


# ---------------------------------------------------------------- 基础
def test_detector_loads_and_reports_ready(detector: DetectorService):
    info = detector.info()
    assert info["loaded"] is True
    assert info["num_classes"] == 15
    assert info["device"] in ("cpu", "cuda:0")


def test_infer_returns_well_formed_detections(detector: DetectorService, image: np.ndarray):
    result = detector.infer(image)

    assert result.inference_ms > 0
    assert len(result.detections) >= 1, "数据集内的图片应当能检出目标"

    height, width = image.shape[:2]
    for det in result.detections:
        assert det.zh_name, "中文类别名未补全"
        assert 0.0 <= det.score <= 1.0
        x1, y1, x2, y2 = det.bbox
        assert 0 <= x1 < x2 <= width
        assert 0 <= y1 < y2 <= height


# ---------------------------------------------------------------- 参数生效
def test_conf_threshold_actually_takes_effect(detector: DetectorService, image: np.ndarray):
    """核心回归：显式传入的 conf 必须真正影响结果。

    旧实现里 `self.params` 与模块级 `ini_params` 是同一个 dict，
    导致「传参无效但看起来能用」；这里用单调性把行为锁死。
    """
    loose = detector.infer(image, conf=0.25, iou=0.5)
    strict = detector.infer(image, conf=0.9, iou=0.5)

    assert len(strict.detections) <= len(loose.detections)
    # 高阈值下所有框的分数都必须达标
    for det in strict.detections:
        assert det.score >= 0.9


def test_set_params_is_persisted_and_clamped(detector: DetectorService):
    original = detector.conf
    try:
        detector.set_params(conf=0.8)
        assert detector.conf == pytest.approx(0.8)
        detector.set_params(iou=0.4)
        assert detector.iou == pytest.approx(0.4)
        # 越界值应被夹到 [0, 1]
        detector.set_params(conf=5.0, iou=-1.0)
        assert detector.conf == 1.0
        assert detector.iou == 0.0
    finally:
        detector.set_params(conf=original, iou=0.5)


# ---------------------------------------------------------------- 热力图 hook
def test_heatmap_hooks_do_not_leak_across_reloads():
    """反复加载模型不得累积 hook（旧实现会持续泄漏并让热力图错乱）。"""
    backend = TorchBackend(heatmap=True)
    backend.load(str(DEFAULT_WEIGHTS), device="cpu", imgsz=640)
    hooks_after_first = len(backend._hooks)

    backend.load(str(DEFAULT_WEIGHTS), device="cpu", imgsz=640)
    backend.load(str(DEFAULT_WEIGHTS), device="cpu", imgsz=640)
    hooks_after_three = len(backend._hooks)

    assert hooks_after_first <= 1
    assert hooks_after_three <= 1, "hook 数量随加载次数增长即为泄漏"


def test_heatmap_computation_does_not_crash(image: np.ndarray):
    backend = TorchBackend(heatmap=True)
    backend.load(str(DEFAULT_WEIGHTS), device="cpu", imgsz=640)
    backend.infer(image, conf=0.25, iou=0.5)

    heat = backend.compute_heatmap(image)
    if backend.heatmap_enabled:
        assert heat is not None
        assert heat.shape == image.shape
    else:
        # 结构不认识时必须安静关闭，而不是抛异常
        assert heat is None


# ---------------------------------------------------------------- 后端回退
def test_unknown_backend_falls_back_to_torch_with_reason():
    from dataclasses import replace

    settings = load_settings()
    settings = replace(
        settings, inference=replace(settings.inference, backend="rknn")
    )
    service = DetectorService(settings)
    service.load(str(DEFAULT_WEIGHTS))

    info = service.info()
    assert info["name"] == "torch"
    assert "回退" in info.get("fallback_reason", "")


def test_missing_weights_raises_backend_unavailable():
    from app.services.backends.base import BackendUnavailableError

    service = DetectorService(load_settings())
    with pytest.raises(BackendUnavailableError):
        service.load(str(PROJECT_ROOT / "weights" / "definitely-not-here.pt"))
