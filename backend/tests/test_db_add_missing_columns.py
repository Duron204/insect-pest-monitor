# -*- coding: utf-8 -*-
"""`db.add_missing_columns()` —— 启动时给**既有**的库补列。

给这一步写测试，是因为它防的失败是**静默且延后**的：模型加了字段、库没迁移，
`create_all()` 不会补列，于是第一次查询整行时 SQLAlchemy 抛
`no such column: devices.agent_streaming` —— 整个设备列表 500。
而这台系统是靠"把文件夹拷到树莓派上再重启"部署的，这个 500 会在**现场**、
在没人跑过迁移脚本的机器上才炸出来。

所以这里守三件事：
  * 旧库能补上、补完能查（`test_a_legacy_database_is_repaired_on_startup`）
  * 重复跑不出事、不重复加（`test_it_is_idempotent`）
  * **非空 / 带默认值的列不许自动补**（那要整表重建，必须交给人确认）
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from app.db import add_missing_columns, create_all, get_engine, init_engine
from app.models import Device

#: 模型里"盒子自报诊断"那一组列。改名时这里会红 —— 那正是想要的效果：
#: 这一组列是**线上已经在用的库**要靠它补出来的，不能悄悄改名。
AGENT_COLUMNS = (
    "agent_streaming",
    "agent_stream_enabled",
    "agent_live_detection",
    "agent_frames_sent",
    "agent_uploads",
    "agent_pending_uploads",
    "agent_last_error",
    "agent_last_error_at",
    "agent_status_at",
)


def _legacy_db(tmp_path, with_created_at: bool = True):
    """造一个"升级前"的库：devices 表在，新列一个都没有。

    `with_created_at=False` 用来造出"连一个带默认值的老列也缺"的库 ——
    那是自动补列**必须放手**的场景（见 test_columns_that_need_a_rebuild_are_refused）。
    """
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    try:
        columns = (
            "id INTEGER PRIMARY KEY, hardware_id VARCHAR(64), name VARCHAR(96),"
            "status VARCHAR(16)"
        )
        values = ("pi-serial-legacy", "东头盒子", "active")
        if with_created_at:
            # 真实的旧库里这列是有的；带上它，"补完之后能查"才是可验的
            columns += ", created_at DATETIME"
            values += ("2026-10-01 08:00:00",)
        con.execute(f"CREATE TABLE devices ({columns})")
        con.execute(
            "INSERT INTO devices (hardware_id, name, status"
            + (", created_at" if with_created_at else "")
            + ") VALUES ("
            + ", ".join("?" * len(values))
            + ")",
            values,
        )
        con.commit()
    finally:
        con.close()
    return path


def _columns(table: str = "devices") -> set[str]:
    with get_engine().begin() as connection:
        return {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}


@pytest.fixture()
def legacy(tmp_path):
    path = _legacy_db(tmp_path)
    init_engine(f"sqlite:///{path.as_posix()}")
    create_all()  # 只建缺失的表，不给已存在的 devices 补列
    yield path
    get_engine().dispose()


def test_a_legacy_database_is_repaired_on_startup(legacy):
    """⭐ 旧库补完之后必须**查得出数据** —— 这正是那个 500 的反面。"""
    assert "agent_streaming" not in _columns(), "前提：旧库里确实没有这一列"

    added = add_missing_columns()
    for name in AGENT_COLUMNS:
        assert f"devices.{name}" in added, f"{name} 没有被补上"
    present = _columns()
    for name in AGENT_COLUMNS:
        assert name in present

    # 关键：整行查询不再炸，且老数据一条没少
    from app.db import session_factory

    session = session_factory()()
    try:
        rows = session.query(Device).all()
        assert len(rows) == 1
        assert rows[0].hardware_id == "pi-serial-legacy"
        assert rows[0].agent_streaming is None, "补出来的列必须是 NULL，不是编造的 false"
    finally:
        session.close()


def test_it_is_idempotent(legacy):
    """补第二次必须什么都不做 —— 启动时每次都跑，重复 ALTER 会直接报错。"""
    assert add_missing_columns(), "第一次应当真的补了列"
    assert add_missing_columns() == [], "第二次必须什么都不做"


def test_columns_that_need_a_rebuild_are_refused(tmp_path, caplog):
    """非空 / 带默认值的列**不许**自动补。

    `devices.created_at` 带 Python 侧默认值：给它 `ADD COLUMN`（不带默认值）在
    SQLite 里语法上允许，但既有行会变成 NULL，而模型根本没打算让它为空 ——
    一颗延时雷。这类列必须留给 `tools/` 下的迁移脚本人工确认。
    """
    init_engine(f"sqlite:///{_legacy_db(tmp_path, with_created_at=False).as_posix()}")
    create_all()

    with caplog.at_level("WARNING", logger="insect.db"):
        add_missing_columns()

    assert "created_at" not in _columns(), "带默认值的列不许被自动补上"
    assert any("created_at" in record.message for record in caplog.records)
