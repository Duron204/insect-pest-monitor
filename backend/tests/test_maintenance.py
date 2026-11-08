# -*- coding: utf-8 -*-
"""运行期维护的测试：产物清理与数据库备份。

这两件事此前**从未实现**（`retention_days` / `keep_uploads` 只是声明），
所以这里的重点是钉住行为本身，而不只是"函数能跑"：

  * 过期任务的产物被删掉、并且 `result_image_path` 被置空
    —— 否则页面会去 `/media/outputs/...` 取一个已不存在的文件
  * 没过期的、以及正在排队/运行的任务**绝不能被清理**
  * `retention_days <= 0` 时整个清理关闭（排查问题要保留现场）
  * 孤儿产物目录、过期上传文件也会被清掉
  * 备份是**可用的**：完整性校验通过、行数与源库一致、轮转保留 N 份
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import DatabaseConfig, Settings
from app.db import create_all, init_engine, session_factory
from app.models import Detection, Task, TaskMode, TaskStatus, User, mtime_utc, utcnow
from app.services import maintenance


def _make_settings(tmp_path: Path, settings: Settings, **storage) -> Settings:
    return replace(
        settings,
        data_dir=tmp_path / "data",
        database=DatabaseConfig(url=f"sqlite:///{(tmp_path / 'data' / 'app.db').as_posix()}"),
        storage=replace(
            settings.storage,
            outputs_dir=str(tmp_path / "outputs"),
            uploads_dir=str(tmp_path / "uploads"),
            **storage,
        ),
    )


def _init(cfg: Settings) -> None:
    """按给定配置建库（与 create_app 里的做法一致）。"""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    init_engine(cfg.resolved_database_url)
    create_all()


def _make_task(session, *, age_days: int, status: str, with_artifacts: bool) -> Task:
    """造一个任务；产物路径必须在拿到 id 之后再写，否则路径与目录对不上。"""
    task = Task(
        mode=TaskMode.IMAGE,
        status=status,
        source_name=f"age{age_days}",
        started_at=utcnow() - timedelta(days=age_days),
        conf=0.25,
        iou=0.5,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    if with_artifacts:
        task.result_image_path = f"outputs/{task.id:06d}/annotated.jpg"
        session.commit()
        session.refresh(task)
    return task


def _write_artifacts(settings: Settings, task_id: int, payload: bytes = b"x" * 100) -> Path:
    directory = settings.abs_outputs_dir / f"{task_id:06d}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "original.jpg").write_bytes(payload)
    (directory / "annotated.jpg").write_bytes(payload)
    return directory


@pytest.fixture()
def env(tmp_path: Path, settings: Settings):
    cfg = _make_settings(tmp_path, settings, retention_days=30, backup_keep=3)
    _init(cfg)
    session = session_factory()()
    try:
        yield cfg, session
    finally:
        session.close()


# ---------------------------------------------------------------- 清理
def test_purge_removes_expired_artifacts_and_clears_paths(env):
    cfg, session = env
    old = _make_task(session, age_days=40, status=TaskStatus.DONE, with_artifacts=True)
    _write_artifacts(cfg, old.id)

    fresh = _make_task(session, age_days=1, status=TaskStatus.DONE, with_artifacts=True)
    _write_artifacts(cfg, fresh.id)

    summary = maintenance.purge_artifacts(cfg, session)

    assert summary["tasks"] == 1
    assert summary["files"] == 2
    assert not (cfg.abs_outputs_dir / f"{old.id:06d}").exists(), "过期产物应被删除"
    assert (cfg.abs_outputs_dir / f"{fresh.id:06d}").exists(), "未过期产物不能被删"

    session.refresh(old)
    assert old.result_image_path is None, "产物删了却留着路径 → 页面会去取不存在的文件"
    session.refresh(fresh)
    assert fresh.result_image_path is not None, "未过期任务的路径不该被动"


def test_purge_skips_active_tasks(env):
    """排队/运行中的任务产物可能还在写 —— 任何情况下都不能删。"""
    cfg, session = env
    running = _make_task(session, age_days=40, status=TaskStatus.RUNNING, with_artifacts=True)
    _write_artifacts(cfg, running.id)

    summary = maintenance.purge_artifacts(cfg, session)
    assert summary["tasks"] == 0
    assert (cfg.abs_outputs_dir / f"{running.id:06d}").exists()


def test_purge_disabled_when_retention_non_positive(tmp_path, settings):
    cfg = _make_settings(tmp_path, settings, retention_days=0)
    _init(cfg)
    session = session_factory()()
    try:
        old = _make_task(session, age_days=999, status=TaskStatus.DONE, with_artifacts=True)
        _write_artifacts(cfg, old.id)
        summary = maintenance.purge_artifacts(cfg, session)
        assert summary["enabled"] is False
        assert (cfg.abs_outputs_dir / f"{old.id:06d}").exists(), "关闭清理时不该删任何东西"
    finally:
        session.close()


def test_purge_cleans_orphan_dirs_and_expired_uploads(env):
    cfg, session = env
    orphan = cfg.abs_outputs_dir / "999999"
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "annotated.jpg").write_bytes(b"y" * 10)

    cfg.abs_uploads_dir.mkdir(parents=True, exist_ok=True)
    stale = cfg.abs_uploads_dir / "old.mp4"
    stale.write_bytes(b"z" * 50)
    old_ts = time.time() - 60 * 24 * 3600
    os.utime(stale, (old_ts, old_ts))  # 把 mtime 拨回 60 天前
    fresh_upload = cfg.abs_uploads_dir / "new.mp4"
    fresh_upload.write_bytes(b"w" * 20)

    summary = maintenance.purge_artifacts(cfg, session)
    assert summary["orphan_dirs"] == 1
    assert not orphan.exists()
    assert summary["uploads"] == 1
    assert not stale.exists()
    assert fresh_upload.exists(), "未过期的上传不能被删"


# ---------------------------------------------------------------- ⭐ 失败任务的产物
def test_purge_reclaims_artifacts_of_a_failed_task(env):
    """⭐ 失败的任务也要回收 —— 磁盘上确实有东西，只是没回填 result 路径。

    `process_video_task` 是**边跑边写** `annotated.mp4` 的，只有成功才回填
    `result_video_path`。所以一个跑到一半崩掉的任务是 failed、两个 result 字段
    都是 NULL，磁盘上却躺着几百 MB 的 mp4。

    判据若写成"记了 result 路径才算有产物"，这类任务会被第 1 步跳过
    （"它没有产物"），第 2 步又因为任务行还在而跳过 —— 两个分支都进不去，
    目录**永远回收不掉**，正是本模块要防的事。所以判据必须**或**上一个
    "产物目录真的存在"。
    """
    cfg, session = env
    task = _make_task(session, age_days=40, status=TaskStatus.FAILED, with_artifacts=False)
    assert task.result_image_path is None and task.result_video_path is None, (
        "这条用例的前提就是「路径没回填」"
    )

    # 复现"边跑边写"留下的半成品：目录与文件都在，只是没回填到任务行上
    directory = cfg.abs_outputs_dir / f"{task.id:06d}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "annotated.mp4").write_bytes(b"v" * 4096)

    summary = maintenance.purge_artifacts(cfg, session)

    assert summary["tasks"] == 1
    assert summary["files"] >= 1
    assert not directory.exists(), "失败任务留在磁盘上的半成品必须被回收"


# ---------------------------------------------------------------- ⭐ 文件时间按 UTC 比
def test_mtime_utc_returns_the_utc_wall_clock(tmp_path):
    """文件时间必须换算成 naive UTC，不是本机时区的时间。

    全库的 naive datetime 都是 UTC（`models.py` 的文件头就是这条约定），
    所以拿 `datetime.fromtimestamp(ts)` 的**本地**时间去比，差值就是这台机器的
    UTC 偏移：UTC+8 上过期文件晚 8 小时才删，UTC 以西的机器则**提前**删掉
    还没到期的文件。树莓派上的 `TZ` 取决于部署，不能靠运气。
    """
    target = tmp_path / "some.bin"
    target.write_bytes(b"x")
    epoch = target.stat().st_mtime

    assert mtime_utc(target) == datetime.fromtimestamp(epoch, tz=timezone.utc).replace(
        tzinfo=None
    )

    # 只有在非 UTC 的机器上这两种算法才分得开 —— 恰是本机（UTC+8）与树莓派的情形。
    # 在 UTC 的 CI 上这一段恒真，它保护的是真实部署。
    offset_hours = -time.timezone / 3600
    if offset_hours != 0:
        assert mtime_utc(target) != datetime.fromtimestamp(epoch), (
            f"本机时区是 UTC{offset_hours:+g}，两个算法必须差一整个偏移"
        )


def test_upload_cleanup_compares_against_utc_not_local_time(env):
    """⭐ 清理的"过期"线必须落在 UTC 上。

    用一个**未来一小时**的 cutoff 把时区混淆放大出来：刚写下的文件按 UTC
    当然比"一小时后"更早，必须判为过期；而按本机时间（UTC+8）算它会比
    "一小时后"更晚，于是被判成没过期 —— 这条断言就是为了抓住那 8 小时。
    """
    cfg, _session = env
    cfg.abs_uploads_dir.mkdir(parents=True, exist_ok=True)
    fresh = cfg.abs_uploads_dir / "just-uploaded.mp4"
    fresh.write_bytes(b"w" * 20)

    expired = maintenance._expired_uploads(cfg, utcnow() + timedelta(hours=1))

    assert fresh in expired, "刚写下的文件在「一小时后」这条线面前必须算过期（按 UTC 比）"


def test_preview_purge_does_not_touch_anything(env):
    cfg, session = env
    old = _make_task(session, age_days=40, status=TaskStatus.DONE, with_artifacts=True)
    directory = _write_artifacts(cfg, old.id)

    info = maintenance.preview_purge(cfg, session)
    assert info["enabled"] is True
    assert info["tasks"] == 1
    assert info["files"] == 2
    assert directory.exists(), "干跑不能删任何文件"
    session.refresh(old)
    assert old.result_image_path is not None


# ---------------------------------------------------------------- 备份
def test_backup_is_usable_and_rotates(env):
    cfg, session = env
    session.add(User(username="backupuser", password_hash="x"))
    _make_task(session, age_days=1, status=TaskStatus.DONE, with_artifacts=False)
    session.commit()

    first = maintenance.backup_database(cfg, verify=True)
    assert Path(first["path"]).is_file()
    assert first["integrity"] == "ok", "备份必须通过完整性校验"
    # 行数与源库一致 —— 证明备份是"完整的库"而不是一个空壳
    assert first["counts"]["users"] == 1
    assert first["counts"]["tasks"] == 1

    # 再备两份 → 超过 keep=3 之前的轮转逻辑：这里只有 3 份，不该删
    for _ in range(2):
        maintenance.backup_database(cfg, verify=False)
    directory = Path(first["path"]).parent
    assert len(list(directory.glob("app-*.db"))) == 3

    # 第 4 份会触发轮转，保留最近 3 份
    fourth = maintenance.backup_database(cfg, verify=False)
    assert fourth["removed"], "超过保留份数应轮转删除最旧的一份"
    assert len(list(directory.glob("app-*.db"))) == 3


def test_backup_reports_missing_database(tmp_path, settings):
    cfg = _make_settings(tmp_path, settings)
    with pytest.raises(FileNotFoundError):
        maintenance.backup_database(cfg)
