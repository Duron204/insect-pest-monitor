# -*- coding: utf-8 -*-
"""运行期维护：产物清理与数据库备份。

**为什么需要**（这三件事此前都只是"声明"，没有实现）：

* `storage.retention_days` 从未被任何代码读取 —— 现场 7×24 跑下去 `outputs/`
  只增不减，而 `deploy/README-pi.md` 却把它当作限制 SD 卡增长的手段推荐；
* `storage.keep_uploads` 同样从未被读取 —— 上传的视频处理完也不删，
  **一个 500MB 的视频就在树莓派上永久占 500MB**（图片/批量走内存不落盘，只有视频落盘）；
* 现场只有一个 `app.db`（账号、历史、告警都在里面），SD 卡坏掉就全丢，
  而代码里没有任何备份机制。

**设计取舍**：

* 清理**只删产物文件，不删任务行** —— 历史、看板、报告要用的数字留着，清掉的是图片与视频。
  因此清理时必须同步把 `result_image_path` / `result_video_path` 置空，
  否则页面会去 `/media/outputs/...` 取一个已经不存在的文件。
* `retention_days <= 0` 表示**不清理**（显式关闭），便于排查问题时保留现场。
* 排队/运行中的任务绝不动。
* 备份用 SQLite 的**在线备份 API**（`Connection.backup`）而不是复制文件：
  服务正在写入时直接 copy 可能拷到不一致的中间状态，而这个 API 是原子的、且**不用停服务**。
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from ..config import Settings
from ..models import Task, TaskStatus, mtime_utc, utcnow
from .device_ingest import prune_ledger
from .storage import remove_task_dir

__all__ = ["sqlite_path", "measure_dir", "preview_purge", "purge_artifacts", "backup_database"]

#: 这些状态的任务可能有正在写入的产物，任何清理都必须跳过
_ACTIVE_STATUS = (TaskStatus.PENDING, TaskStatus.RUNNING)


def sqlite_path(settings: Settings) -> Path:
    """从配置里解析出 SQLite 文件路径（非 sqlite 或内存库直接报错）。"""
    url = settings.resolved_database_url
    if not url.startswith("sqlite:///"):
        raise ValueError(f"只支持文件型 sqlite 数据库，当前是：{url}")
    return Path(url[len("sqlite:///") :])


def measure_dir(path: Path) -> tuple[int, int]:
    """返回 (文件数, 字节数)；路径不存在时返回 (0, 0)。"""
    if not path.exists():
        return 0, 0
    files = 0
    size = 0
    for child in path.rglob("*"):
        if child.is_file():
            files += 1
            try:
                size += child.stat().st_size
            except OSError:
                continue
    return files, size


def _expired_uploads(settings: Settings, cutoff: datetime) -> list[Path]:
    uploads = settings.abs_uploads_dir
    if not uploads.is_dir():
        return []
    expired: list[Path] = []
    for child in uploads.iterdir():
        if not child.is_file():
            continue
        try:
            if mtime_utc(child) < cutoff:
                expired.append(child)
        except OSError:
            continue
    return expired


def preview_purge(
    settings: Settings,
    db: OrmSession,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """干跑：只统计"将会被清理多少"，不改动任何文件或数据库行。"""
    retention = int(settings.storage.retention_days)
    if retention <= 0:
        return {"enabled": False, "reason": "retention_days<=0，已显式关闭清理"}

    cutoff = (now or utcnow()) - timedelta(days=retention)
    tasks = 0
    files = 0
    size = 0
    for task in db.scalars(select(Task).where(Task.started_at < cutoff)).all():
        if task.status in _ACTIVE_STATUS:
            continue
        directory = settings.abs_outputs_dir / f"{task.id:06d}"
        # "有产物可清" = 记了 result 路径，**或者**磁盘上真的有目录。
        # 后一个条件不能少：失败的任务没有 result 路径，却可能留下过半个文件
        # （见 purge_artifacts 里的详细说明）。干跑与实跑必须用同一套判据，
        # 否则"预计清理"和"实际清理"会对不上。
        if not (task.result_image_path or task.result_video_path) and not directory.is_dir():
            continue
        got_files, got_size = measure_dir(directory)
        tasks += 1
        files += got_files
        size += got_size

    orphan_dirs = 0
    outputs = settings.abs_outputs_dir
    if outputs.is_dir():
        known = set(db.scalars(select(Task.id)).all())
        for child in outputs.iterdir():
            if child.is_dir() and child.name.isdigit() and int(child.name) not in known:
                got_files, got_size = measure_dir(child)
                orphan_dirs += 1
                files += got_files
                size += got_size

    uploads = _expired_uploads(settings, cutoff)
    upload_bytes = 0
    for path in uploads:
        try:
            upload_bytes += path.stat().st_size
        except OSError:
            continue

    return {
        "enabled": True,
        "cutoff": cutoff.isoformat(),
        "tasks": tasks,
        "orphan_dirs": orphan_dirs,
        "uploads": len(uploads),
        "files": files,
        "bytes": size,
        "upload_bytes": upload_bytes,
    }


def purge_artifacts(
    settings: Settings,
    db: OrmSession,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按 `retention_days` 清理过期产物与上传文件，返回统计结果。"""
    retention = int(settings.storage.retention_days)
    if retention <= 0:
        return {
            "enabled": False,
            "reason": "retention_days<=0，已显式关闭清理",
            "tasks": 0,
            "orphan_dirs": 0,
            "uploads": 0,
            "files": 0,
            "bytes": 0,
        }

    cutoff = (now or utcnow()) - timedelta(days=retention)
    summary: dict[str, Any] = {
        "enabled": True,
        "cutoff": cutoff.isoformat(),
        "tasks": 0,
        "orphan_dirs": 0,
        "uploads": 0,
        "files": 0,
        "bytes": 0,
    }

    def _drop_task_dir(task_id: int) -> tuple[int, int]:
        """删掉某任务的产物目录，返回 (文件数, 字节数)。"""
        directory = settings.abs_outputs_dir / f"{task_id:06d}"
        files, size = measure_dir(directory)
        remove_task_dir(settings, task_id)
        return files, size

    # 1) 过期任务的产物
    #
    # 判据是"记了 result 路径**或**产物目录真的存在"，**不是**"记了 result 路径"。
    # 原判据漏掉了失败的任务：`process_video_task` 是边跑边写 annotated.mp4 的，
    # 只有成功才回填 `result_video_path`（video.py），所以一个跑到 90% 崩掉的视频
    # 任务状态是 failed、两个 result 字段都是 NULL，磁盘上却躺着几百 MB 的 mp4 ——
    # 它两个分支都进不去：这里因为"没有产物"被跳过，第 2 步又因为任务行还在而跳过。
    # 于是这类目录**永远不被回收**，正是本模块要防的事。
    rows = db.scalars(select(Task).where(Task.started_at < cutoff)).all()
    for task in rows:
        if task.status in _ACTIVE_STATUS:
            continue  # 正在跑的任务，产物可能还在写
        directory = settings.abs_outputs_dir / f"{task.id:06d}"
        if not (task.result_image_path or task.result_video_path) and not directory.is_dir():
            continue  # 真的没有产物
        files, size = _drop_task_dir(task.id)
        task.result_image_path = None
        task.result_video_path = None
        summary["tasks"] += 1
        summary["files"] += files
        summary["bytes"] += size
    db.commit()

    # 2) 孤儿产物目录（任务行已不存在，例如手工删过库）
    outputs = settings.abs_outputs_dir
    if outputs.is_dir():
        known = set(db.scalars(select(Task.id)).all())
        for child in outputs.iterdir():
            if not child.is_dir() or not child.name.isdigit():
                continue
            if int(child.name) in known:
                continue
            files, size = measure_dir(child)
            shutil.rmtree(child, ignore_errors=True)
            summary["orphan_dirs"] += 1
            summary["files"] += files
            summary["bytes"] += size

    # 3) 过期上传文件（keep_uploads=true 时才会长期留在这里）
    uploads = settings.abs_uploads_dir
    if uploads.is_dir():
        for child in uploads.iterdir():
            if not child.is_file():
                continue
            try:
                if mtime_utc(child) >= cutoff:
                    continue
                size = child.stat().st_size
                child.unlink()
            except OSError:
                continue
            summary["uploads"] += 1
            summary["files"] += 1
            summary["bytes"] += size

    # 4) 设备上报的幂等台账。它只需要覆盖重发窗口（几十分钟），
    #    但会跟着抓拍次数一直长，所以挂在这里一起剪。**只删台账行，不删任务** ——
    #    台账回答的是"这条 seq 收过没有"，历史记录与它是两回事。
    summary["ledger"] = prune_ledger(db, settings, now=now)

    return summary


def backup_database(
    settings: Settings,
    *,
    keep: int | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """在线备份 sqlite 数据库，并轮转旧备份（默认保留 `storage.backup_keep` 份）。"""
    source = sqlite_path(settings)
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在：{source}")

    keep_count = int(settings.storage.backup_keep if keep is None else keep)
    directory = source.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = directory / f"{source.stem}-{stamp}.db"
    # 时间戳只到秒：同一秒内连续备份（重试、手工连跑、测试）会互相覆盖。
    # 同名就往后加序号，保证每次备份都独立存在。
    suffix = 1
    while target.exists():
        suffix += 1
        target = directory / f"{source.stem}-{stamp}-{suffix}.db"

    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)  # 在线备份：不需要停服务，也不会拷到中间状态
        finally:
            dst.close()
    finally:
        src.close()

    counts: dict[str, int] = {}
    integrity: str | None = None
    if verify:
        con = sqlite3.connect(str(target))
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            for table in ("users", "tasks", "detections", "alerts"):
                try:
                    counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    counts[table] = -1
        finally:
            con.close()

    removed: list[str] = []
    if keep_count > 0:
        backups = sorted(directory.glob(f"{source.stem}-*.db"))
        for old in backups[:-keep_count]:
            try:
                old.unlink()
            except OSError:
                continue
            removed.append(old.name)

    return {
        "path": str(target),
        "bytes": target.stat().st_size if target.exists() else 0,
        "integrity": integrity,
        "counts": counts,
        "removed": removed,
        "keep": keep_count,
    }
