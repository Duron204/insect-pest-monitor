# -*- coding: utf-8 -*-
"""图像读写与绘制。

**中文路径安全**：项目位于 `D:\\VScode project\\病虫害\\...`，
而 `cv2.imread` / `cv2.imwrite` 在非 ASCII 路径上会静默失败
（旧项目 `deal_data_txt.py:33` 正是踩了这个坑，返回 None 后紧接 `image.shape` 崩溃）。
这里统一走 `np.fromfile` / `imencode().tofile()`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

# 与前端 CJK 字体无关的绘制配色（BGR）：按类别 id 取模，保证同类同色
_PALETTE: tuple[tuple[int, int, int], ...] = (
    (95, 219, 61),    # 绿
    (61, 163, 232),   # 琥珀
    (232, 120, 61),   # 蓝
    (200, 80, 200),   # 紫
    (60, 200, 220),   # 黄
    (150, 150, 250),  # 红
    (110, 190, 90),
    (220, 180, 70),
)


def read_image(path: str | Path) -> np.ndarray | None:
    """读取图像，支持中文路径。失败返回 None（不抛异常）。"""
    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buffer.size == 0:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def save_image(path: str | Path, image: np.ndarray, quality: int = 92) -> bool:
    """保存图像，支持中文路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix.lower() or ".jpg"
    params: list[int] = []
    if suffix in (".jpg", ".jpeg"):
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    elif suffix == ".png":
        params = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
    ok, buffer = cv2.imencode(suffix, image, params)
    if not ok:
        return False
    try:
        buffer.tofile(str(target))
    except OSError:
        return False
    return True


def decode_image(data: bytes) -> np.ndarray | None:
    """从字节流解码图像（用于 multipart 上传与 base64 帧）。"""
    if not data:
        return None
    buffer = np.frombuffer(data, dtype=np.uint8)
    if buffer.size == 0:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def encode_jpeg(image: np.ndarray, quality: int = 70) -> bytes | None:
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buffer.tobytes() if ok else None


def color_for(class_id: int) -> tuple[int, int, int]:
    return _PALETTE[int(class_id) % len(_PALETTE)]


def draw_detections(
    image: np.ndarray,
    detections: Iterable[object],
    *,
    draw_label: bool = True,
) -> np.ndarray:
    """在图上画框与标签（返回新图，不修改入参）。

    标签用英文+置信度：OpenCV 自带字体不支持中文，
    中文标签交给前端 canvas 绘制（也顺带让框可交互）。
    """
    canvas = image.copy()
    for det in detections:
        bbox = getattr(det, "bbox", None)
        if bbox is None and isinstance(det, dict):
            bbox = det.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = (int(v) for v in bbox[:4])
        class_id = int(getattr(det, "class_id", 0) if not isinstance(det, dict) else det.get("class_id", 0))
        score = float(getattr(det, "score", 0.0) if not isinstance(det, dict) else det.get("score", 0.0))
        name = getattr(det, "class_name", "") if not isinstance(det, dict) else det.get("class_name", "")
        color = color_for(class_id)

        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)
        if draw_label:
            text = f"{name} {score:.2f}" if name else f"{score:.2f}"
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            top = max(0, y1 - th - 8)
            cv2.rectangle(canvas, (x1, top), (x1 + tw + 8, top + th + 8), color, -1)
            cv2.putText(
                canvas,
                text,
                (x1 + 4, top + th + 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (20, 20, 20),
                1,
                cv2.LINE_AA,
            )
    return canvas


def resize_keep_ratio(image: np.ndarray, max_side: int) -> np.ndarray:
    """等比缩放到最长边不超过 max_side（用于设备上传省流量）。"""
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return image
    scale = max_side / float(longest)
    return cv2.resize(
        image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA
    )


def to_base64_jpeg(image: np.ndarray, quality: int = 70) -> str | None:
    import base64

    payload = encode_jpeg(image, quality)
    return base64.b64encode(payload).decode("ascii") if payload else None


def from_base64_image(payload: str) -> np.ndarray | None:
    """解码前端/设备传来的 base64 帧；容忍带 data URL 前缀。"""
    import base64

    if not payload:
        return None
    text = payload.split(",", 1)[-1] if payload.startswith("data:") else payload
    try:
        raw = base64.b64decode(text, validate=False)
    except (ValueError, TypeError):
        return None
    return decode_image(raw)


def count_by_class(detections: Sequence[object]) -> dict[str, int]:
    """按中文名（缺失则英文名）统计数量。"""
    counts: dict[str, int] = {}
    for det in detections:
        if isinstance(det, dict):
            name = det.get("zh_name") or det.get("class_name") or "未知"
        else:
            name = getattr(det, "zh_name", "") or getattr(det, "class_name", "") or "未知"
        counts[name] = counts.get(name, 0) + 1
    return counts
