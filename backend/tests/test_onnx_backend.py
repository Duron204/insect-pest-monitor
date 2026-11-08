# -*- coding: utf-8 -*-
"""ONNX 后端与 letterbox / NMS 的回归测试。

核心是**预处理必须与 ultralytics 逐像素一致** —— ONNX 与 torch 的检出结果
能否对齐，根因就在这里。有 ultralytics 时直接与它的 `LetterBox` 对比；
没有时退化为形状与补边数值的断言（仍能挡住回归）。

本文件**不要求安装 onnxruntime**：除了显式验证"缺依赖时报错"的用例，
其余都是纯 numpy 逻辑，因此在只有 torch 的开发环境里也会跑。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.backends.base import BackendUnavailableError
from app.services.backends.onnx_backend import letterbox, nms_indices

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE = PROJECT_ROOT / "test_media" / "InsectPest_SIXU_A00025.jpg"

# 覆盖放大 / 缩小 / 上下补边 / 左右补边 / 不补边
SIZES = [(416, 416), (800, 300), (300, 800), (200, 200), (1280, 1280), (640, 480)]


def _image(width: int, height: int) -> np.ndarray:
    """中文路径安全读图。"""
    buffer = np.fromfile(str(TEST_IMAGE), dtype=np.uint8)
    base = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    assert base is not None
    return cv2.resize(base, (width, height), interpolation=cv2.INTER_LINEAR)


def _has_onnxruntime() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# ---------------------------------------------------------------- letterbox
@pytest.mark.parametrize("size", SIZES)
def test_letterbox_is_pixel_identical_to_ultralytics(size):
    """与 ultralytics 的 LetterBox 逐像素一致。

    这是 ONNX 结果能与 torch 对齐的前提：任何补边/插值/取整的差异都会
    让框偏移，而且症状是"能跑但框偏一点"，很难察觉。
    """
    augment = pytest.importorskip("ultralytics.data.augment")
    image = _image(*size)
    reference = augment.LetterBox(640, auto=False, stride=32)(image=image)
    mine, ratio, pad_x, pad_y = letterbox(image, 640)

    assert mine.shape == reference.shape
    assert np.array_equal(mine, reference), f"{size} 的 letterbox 与 ultralytics 不一致"
    assert ratio > 0 and pad_x >= 0 and pad_y >= 0


def test_letterbox_pads_bottom_and_top_with_114_gray():
    """800x300 -> 640x240，上下各补 200 像素的 (114,114,114)。"""
    padded, ratio, pad_x, pad_y = letterbox(_image(800, 300), 640)

    assert padded.shape == (640, 640, 3)
    assert ratio == pytest.approx(0.8)
    assert pad_x == 0.0
    assert pad_y == pytest.approx(200.0, abs=1.0)
    for row in (0, 199, 639):
        assert tuple(int(v) for v in padded[row, 320]) == (114, 114, 114)
    # 中间那一行是真实图像内容，不应还是纯灰
    assert tuple(int(v) for v in padded[320, 320]) != (114, 114, 114)


def test_letterbox_does_not_pad_square_input():
    padded, ratio, pad_x, pad_y = letterbox(_image(416, 416), 640)
    assert padded.shape == (640, 640, 3)
    assert pad_x == 0 and pad_y == 0
    assert ratio == pytest.approx(640 / 416)


def test_letterbox_scaleup_false_keeps_small_image_unscaled():
    padded, ratio, _, _ = letterbox(_image(200, 200), 640, scaleup=False)
    assert ratio == 1.0
    assert padded.shape == (640, 640, 3)


# ---------------------------------------------------------------- NMS
def test_nms_suppresses_overlapping_and_keeps_distant():
    boxes = np.array(
        [[0, 0, 100, 100], [5, 5, 105, 105], [500, 500, 600, 600]], dtype=np.float64
    )
    scores = np.array([0.9, 0.8, 0.7])
    assert nms_indices(boxes, scores, 0.5, 300) == [0, 2]


def test_nms_keeps_all_when_nothing_overlaps():
    boxes = np.array([[0, 0, 10, 10], [100, 100, 110, 110]], dtype=np.float64)
    scores = np.array([0.5, 0.6])
    keep = nms_indices(boxes, scores, 0.5, 300)
    assert sorted(keep) == [0, 1]


def test_nms_respects_max_det():
    boxes = np.array(
        [[i * 300, 0, i * 300 + 50, 50] for i in range(6)], dtype=np.float64
    )
    scores = np.linspace(0.9, 0.4, 6)
    assert len(nms_indices(boxes, scores, 0.5, 2)) == 2


def test_nms_on_empty_input_returns_empty():
    assert nms_indices(np.zeros((0, 4)), np.zeros((0,)), 0.5, 300) == []


# ---------------------------------------------------------------- 依赖缺失时的行为
def test_onnx_backend_raises_unavailable_when_runtime_missing(monkeypatch):
    """未装 onnxruntime 时应抛 BackendUnavailableError，供 detector 决定回退。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("模拟未安装 onnxruntime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from app.services.backends.onnx_backend import OnnxBackend

    with pytest.raises(BackendUnavailableError):
        OnnxBackend().load("whatever.onnx", "cpu", 640)


@pytest.mark.skipif(_has_onnxruntime(), reason="本环境已装 onnxruntime，无法验证回退路径")
def test_detector_falls_back_to_torch_and_records_reason():
    """配了 onnx 但缺依赖时必须回退 torch，并把原因记录下来（不能静默降级）。"""
    from dataclasses import replace

    from app.config import load_settings
    from app.services.detector import DetectorService

    settings = load_settings()
    inference = replace(settings.inference, backend="onnx", preload=False)
    service = DetectorService(replace(settings, inference=inference))
    backend = service._create_backend("onnx")  # noqa: SLF001 - 直接验证工厂的回退行为

    assert backend.name == "torch"
    assert service._fallback_reason, "回退必须留下原因"  # noqa: SLF001
    assert "onnx" in service._fallback_reason.lower()  # noqa: SLF001
