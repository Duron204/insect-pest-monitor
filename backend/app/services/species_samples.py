# -*- coding: utf-8 -*-
"""为知识库的每个类别挑一张**数据集实拍图**当参考图，并缓存到后端数据目录。

**为什么需要它**：知识库卡片早就留了配图位置（前端 `SpeciesCard` 支持
`image_path`，取不到就显示"暂无参考图"），但后端从来没有提供过图片 ——
于是 15 张卡片上是 15 个灰色占位框。而数据集里每个类别本来就有大量实拍图。

**为什么要缓存一份**：树莓派上通常只部署模型、不带几百 MB 的训练集。
如果只从数据集现读，卡片到了现场就没图了。所以第一次扫描时把每个类别的
一张图复制到 `data_dir/species_samples/`（15 张、约 450KB），
运行期只从那里读 —— 数据集删了也不影响。

**缓存怎么失效**：把数据集的指纹（每个 split 的标注文件数与最新修改时间）
记进 `_dataset.json`。进程首次用到时比对指纹，不一致就**重新扫描并覆盖**
缓存，同时删掉已经不在数据集里的类别。没有这一步的话，重新标注/重训之后
那 15 张图会永远停在旧版本，而且不会有任何提示（静默过期）。

**扫描成本**：训练集有 6000+ 份标注，逐份读首列不便宜，因此结果缓存在进程内、
只算一次（指纹比对也是一次目录遍历，约几百毫秒）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
from pathlib import Path

from ..config import Settings

__all__ = ["samples_for", "sample_for", "refresh_cache", "reset_cache"]

_SPLITS = ("train", "valid", "test")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
_MANIFEST = "_dataset.json"

_lock = threading.Lock()
_cache: dict[str, dict[int, Path]] = {}


# ---------------------------------------------------------------- 数据集扫描
def _scan_dataset(dataset_root: Path) -> dict[int, Path]:
    """扫标注文件首列（YOLO 的 class id），为每个类别取第一张对应图片。"""
    found: dict[int, Path] = {}
    for split in _SPLITS:
        labels_dir = dataset_root / split / "labels"
        images_dir = dataset_root / split / "images"
        if not labels_dir.is_dir() or not images_dir.is_dir():
            continue
        for label in sorted(labels_dir.glob("*.txt")):
            try:
                text = label.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                parts = line.split()
                if not parts:
                    continue
                try:
                    class_id = int(float(parts[0]))
                except ValueError:
                    continue
                if class_id in found:
                    continue
                for suffix in _IMAGE_SUFFIXES:
                    candidate = images_dir / f"{label.stem}{suffix}"
                    if candidate.is_file():
                        found[class_id] = candidate
                        break
    return found


def _fingerprint(dataset_root: Path) -> str:
    """数据集的指纹：每个 split 的标注文件数 + 最新修改时间。

    标注内容改动会更新文件 mtime，因此"改标注"和"增删图片"都能被发现；
    图片本身换了但标注没动的情况检测不到（那是另一种改动，代价不值得）。
    """
    if not dataset_root.is_dir():
        return ""
    parts: list[str] = []
    for split in _SPLITS:
        labels_dir = dataset_root / split / "labels"
        if not labels_dir.is_dir():
            continue
        count = 0
        newest = 0.0
        for label in labels_dir.glob("*.txt"):
            count += 1
            try:
                newest = max(newest, label.stat().st_mtime)
            except OSError:
                continue
        parts.append(f"{split}:{count}:{newest:.0f}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest() if parts else ""


# ---------------------------------------------------------------- 缓存目录
def _cache_dir(settings: Settings) -> Path:
    return settings.data_dir / "species_samples"


def _manifest_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _MANIFEST


def _read_manifest(settings: Settings) -> str | None:
    try:
        return _manifest_path(settings).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_manifest(settings: Settings, fingerprint: str) -> None:
    try:
        _cache_dir(settings).mkdir(parents=True, exist_ok=True)
        _manifest_path(settings).write_text(fingerprint, encoding="utf-8")
    except OSError:
        # 写不了指纹只是下次会再扫一遍，不影响功能
        pass


def _discover_cached(settings: Settings) -> dict[int, Path]:
    """已经缓存过的类别（文件名就是 class_id，例如 `3.jpg`；清单文件除外）。"""
    directory = _cache_dir(settings)
    found: dict[int, Path] = {}
    if not directory.is_dir():
        return found
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES and path.stem.isdigit():
            found[int(path.stem)] = path
    return found


def _refresh(settings: Settings, scanned: dict[int, Path]) -> dict[int, Path]:
    """按最新扫描结果**覆盖**缓存，并删掉已不在数据集里的类别。"""
    directory = _cache_dir(settings)
    stored: dict[int, Path] = {}
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return dict(scanned)  # 建不了目录就直接用数据集原图

    for class_id, source in scanned.items():
        destination = directory / f"{class_id}{source.suffix.lower()}"
        try:
            shutil.copy2(source, destination)
            stored[class_id] = destination
        except OSError:
            # 复制失败（磁盘满/权限）不该让功能失效：退回直接读数据集原图
            stored[class_id] = source

    # 清理孤儿：类别被改名或从数据集里删掉后，旧缓存不该留下来
    for stale in directory.iterdir():
        if not stale.is_file() or stale.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        if stale.stem.isdigit() and int(stale.stem) not in scanned:
            try:
                stale.unlink()
            except OSError:
                continue
    return stored


def _load(settings: Settings) -> dict[int, Path]:
    dataset_root = settings.project_root / "datasets" / "InsectPest"
    fingerprint = _fingerprint(dataset_root)
    manifest = _read_manifest(settings)

    if fingerprint and fingerprint != manifest:
        # 数据集变了（或第一次跑）：重新扫描并覆盖缓存
        stored = _refresh(settings, _scan_dataset(dataset_root))
        _write_manifest(settings, fingerprint)
        return stored

    # 指纹一致：直接用已有缓存；若缓存为空而数据集在（例如手工删了缓存），补建一次
    stored = _discover_cached(settings)
    if not stored and fingerprint:
        stored = _refresh(settings, _scan_dataset(dataset_root))
        _write_manifest(settings, fingerprint)
    return stored


def samples_for(settings: Settings) -> dict[int, Path]:
    """返回 `class_id -> 参考图路径`（优先用缓存副本；数据集与缓存都没有时为空字典）。"""
    key = str(settings.project_root)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    with _lock:
        cached = _cache.get(key)
        if cached is None:
            cached = _load(settings)
            _cache[key] = cached
    return cached


def sample_for(settings: Settings, class_id: int) -> Path | None:
    return samples_for(settings).get(class_id)


def refresh_cache(settings: Settings) -> dict[int, Path]:
    """强制重新扫描并覆盖缓存（清掉进程内缓存后重建）。"""
    with _lock:
        _cache.pop(str(settings.project_root), None)
    return samples_for(settings)


def reset_cache() -> None:
    """测试用：清掉进程内缓存以便重新加载。"""
    with _lock:
        _cache.clear()
