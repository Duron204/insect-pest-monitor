# -*- coding: utf-8 -*-
"""模型注册表：扫描磁盘上的权重 + 解析训练指标，落库供前端对比与切换。

**为什么需要它**：项目里同时存在 `weights/*.pt` 与
`runs/detect/*/weights/best.pt` 多个权重，而代码各处硬编码同一个文件名。
结果是 mAP50-95 高 3.8 个点的 yolo11s 权重完全闲置。
把「有哪些模型、各自什么成绩、哪个在用」变成数据，前端才能让用户选。

指标取自各训练目录下的 `results.csv`（ultralytics 自动产出），
取所有 epoch 中的**最大值**而非最后一行 —— 训练末段常已过拟合回落。
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from ..config import Settings
from ..models import ModelInfo, mtime_utc

_MAP50_KEYS = ("metrics/mAP50(B)", "metrics/mAP50")
_MAP50_95_KEYS = ("metrics/mAP50-95(B)", "metrics/mAP50-95")

_ARCH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"yolo11([nsmlx])", re.I), "yolo11"),
    (re.compile(r"yolov8([nsmlx])", re.I), "yolov8"),
    (re.compile(r"yolov5([nsmlx])?u?", re.I), "yolov5"),
    # 训练目录名形式，例如 train_v8_InsectPest3 / train_v11_... （无 yolo 前缀）
    (re.compile(r"(?:^|[_\-\s])v11(?:[nsmlx])?(?:[_\-\s]|$)", re.I), "yolo11"),
    (re.compile(r"(?:^|[_\-\s])v8(?:[nsmlx])?(?:[_\-\s]|$)", re.I), "yolov8"),
    (re.compile(r"(?:^|[_\-\s])v5(?:[nsmlx])?(?:[_\-\s]|$)", re.I), "yolov5"),
)


def guess_arch(name: str) -> str | None:
    """从文件名/目录名猜出架构标识，例如 'best-yolov8n1' -> 'yolov8n'。

    注意：部分模式没有捕获组（只表示代次），因此不能无条件读 group(1)。
    """
    for pattern, family in _ARCH_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        size = ""
        if match.re.groups >= 1:
            group = match.group(1)
            size = (group or "").lower()
        return f"{family}{size}" if size else family
    return None


def _cell(row: dict[str, str], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        for raw_key, raw_value in row.items():
            if raw_key and raw_key.strip() == key:
                try:
                    return float(raw_value)
                except (TypeError, ValueError):
                    return None
    return None


def read_metrics(results_csv: Path) -> dict[str, Any]:
    """读取训练指标。返回 {map50, map50_95, epochs}，取历史最优值。"""
    if not results_csv.exists():
        return {}
    try:
        with results_csv.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
            rows = [row for row in csv.DictReader(handle) if row]
    except OSError:
        return {}
    if not rows:
        return {}

    best_50: float | None = None
    best_50_95: float | None = None
    epochs: int | None = None
    for row in rows:
        value = _cell(row, _MAP50_KEYS)
        if value is not None and (best_50 is None or value > best_50):
            best_50 = value
        value = _cell(row, _MAP50_95_KEYS)
        if value is not None and (best_50_95 is None or value > best_50_95):
            best_50_95 = value
        epoch_value = _cell(row, ("epoch",))
        if epoch_value is not None:
            epochs = int(epoch_value)

    return {"map50": best_50, "map50_95": best_50_95, "epochs": epochs}


def _file_time(path: Path) -> datetime | None:
    try:
        return mtime_utc(path)
    except OSError:
        return None


def _sidecar_metrics(file: Path) -> dict[str, Any]:
    """读取与权重同名的 `<stem>.metrics.json`（可选，缺失即视为无指标）。

    **为什么需要它**：指标的正常来源是 `runs/*/results.csv`，只有训练目录里的权重才有。
    一旦把最好的模型挑出来、以规范名放进 `weights/` 作为正式模型，它就脱离了 runs 目录，
    指标随之丢失 —— 模型页会显示"—"，更糟的是 `best_recommendation()` 会因为
    "它没有指标"而去推荐一个精度更差的模型。
    这个旁挂文件让权重自带指标与出处，训练脚本之外也能追溯。
    """
    path = file.with_suffix(".metrics.json")
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    metrics: dict[str, Any] = {}
    for key in ("map50", "map50_95"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            metrics[key] = float(value)
    epochs = payload.get("epochs")
    if isinstance(epochs, int):
        metrics["epochs"] = epochs
    return metrics


def discover(settings: Settings) -> list[dict[str, Any]]:
    """扫描磁盘上所有可用权重。"""
    root = settings.project_root
    found: list[dict[str, Any]] = []

    # 1) weights/*.pt —— 手工放置/导出的权重
    weights_dir = root / "weights"
    if weights_dir.is_dir():
        for file in sorted(weights_dir.glob("*.pt")):
            rel = file.relative_to(root).as_posix()
            metrics = _sidecar_metrics(file)
            found.append(
                {
                    "name": file.stem,
                    "path": rel,
                    "arch": guess_arch(file.stem),
                    "source": "weights",
                    "profile": "edge" if file.parent.name == "edge" else "pc",
                    "trained_at": _file_time(file),
                    "map50": metrics.get("map50"),
                    "map50_95": metrics.get("map50_95"),
                    "epochs": metrics.get("epochs"),
                }
            )

    # 2) ONNX 权重 —— 导出给边缘（树莓派）用的推理格式。
    #
    # 原先这里只扫 .pt，导致**树莓派上模型页是空的**：边缘只部署 .onnx
    # （没装 torch，跑不动 .pt），而注册表又不认 .onnx。
    # 现在两种都列出来，前端按 source 区分显示。
    if weights_dir.is_dir():
        edge_dir = weights_dir / "edge"
        candidates: list[tuple[Path, str, str]] = []
        if edge_dir.is_dir():
            candidates += [(f, "edge", "edge") for f in sorted(edge_dir.glob("*.onnx"))]
        candidates += [(f, "weights", "pc") for f in sorted(weights_dir.glob("*.onnx"))]
        for file, source, profile in candidates:
            rel = file.relative_to(root).as_posix()
            metrics = _sidecar_metrics(file)
            found.append(
                {
                    "name": file.stem,
                    "path": rel,
                    "arch": guess_arch(file.stem),
                    "source": source,
                    "profile": profile,
                    "trained_at": _file_time(file),
                    "map50": metrics.get("map50"),
                    "map50_95": metrics.get("map50_95"),
                    "epochs": metrics.get("epochs"),
                }
            )

    # 3) runs/detect/*/weights/best.pt —— 训练产出，可解析 results.csv 得到指标
    runs_dir = root / "runs" / "detect"
    if runs_dir.is_dir():
        for best in sorted(runs_dir.glob("*/weights/best.pt")):
            run_dir = best.parent.parent
            rel = best.relative_to(root).as_posix()
            metrics = read_metrics(run_dir / "results.csv")
            found.append(
                {
                    "name": f"{run_dir.name}/best",
                    "path": rel,
                    "arch": guess_arch(run_dir.name) or guess_arch(best.name),
                    "source": "runs",
                    "profile": "pc",
                    "trained_at": _file_time(run_dir / "results.csv") or _file_time(best),
                    "map50": metrics.get("map50"),
                    "map50_95": metrics.get("map50_95"),
                    "epochs": metrics.get("epochs"),
                }
            )

    return found


def _assign_unique_names(discovered: list[dict], reserved: set[str]) -> None:
    """就地给每条发现结果定一个不与 `reserved` 冲突的显示名。

    磁盘上重名是**正常**的：`weights/best.pt` 和导出给树莓派的
    `weights/best.onnx` 是同一份权重的两种格式，`file.stem` 都是 `best`；
    而 `ModelInfo.name` 是 unique 的 —— 直接写进去就是 `IntegrityError`，
    模型页整个 500。撞名时补格式后缀（`best` → `best (onnx)`），还撞再加序号。

    名字只由 path 与 `reserved` 决定，**不读库里的旧值、也不依赖遍历顺序**：
    `sync` 每次都会无条件重写 `row.name`，名字一旦依赖上一次的结果，
    两次 sync 之间就会来回抖。
    """
    taken = set(reserved)
    for item in discovered:
        path = Path(item["path"])
        suffix = path.suffix.lstrip(".")
        name = path.stem
        if name in taken:
            name = f"{path.stem} ({suffix})"
            index = 2
            while name in taken:
                name = f"{path.stem} ({suffix}-{index})"
                index += 1
        item["name"] = name
        taken.add(name)


def sync(db: OrmSession, settings: Settings) -> list[ModelInfo]:
    """把磁盘状态同步进库。保留用户已选的 is_default，不覆盖成别的东西。"""
    discovered = discover(settings)
    existing = {row.path: row for row in db.scalars(select(ModelInfo)).all()}
    default_rel = settings.inference.default_model.replace("\\", "/")
    discovered_paths = {item["path"] for item in discovered}

    # 显示名去重（见 `_assign_unique_names`）。文件已被删掉的历史行不参与本次重算，
    # 但它们的名字得先占住 —— 否则一个新文件可能撞上一个只剩在库里的旧名字。
    _assign_unique_names(
        discovered,
        {row.name for path, row in existing.items() if path not in discovered_paths},
    )

    for item in discovered:
        row = existing.get(item["path"])
        if row is None:
            row = ModelInfo(path=item["path"], name=item["name"])
            db.add(row)
            existing[item["path"]] = row
        row.name = item["name"]
        row.arch = item["arch"]
        row.source = item["source"]
        row.profile = item["profile"]
        row.trained_at = item["trained_at"]
        row.map50 = item["map50"]
        row.map50_95 = item["map50_95"]
        row.epochs = item["epochs"]
        row.exists = True

    for path, row in existing.items():
        if path not in discovered_paths:
            row.exists = False

    # is_default 由配置决定；只有一个为真
    for row in existing.values():
        row.is_default = bool(row.exists and row.path == default_rel)

    db.commit()
    return list(existing.values())


def ranked(models: list[ModelInfo]) -> list[ModelInfo]:
    """按 mAP50-95 降序排列，缺失指标的排最后（便于前端直接展示推荐）。"""
    return sorted(
        models,
        key=lambda m: (m.map50_95 is None, -(m.map50_95 or 0.0), m.name),
    )


def best_recommendation(models: list[ModelInfo]) -> ModelInfo | None:
    """返回精度最高的可用模型 —— 用于前端提示「建议切换到它」。"""
    candidates = [m for m in ranked(models) if m.exists and m.map50_95 is not None]
    return candidates[0] if candidates else None
