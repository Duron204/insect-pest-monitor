# -*- coding: utf-8 -*-
"""`tools/migrate_add_ownership.py` 的回归测试。

给迁移脚本写测试的理由与 `test_migrate_add_devices.py` 相同：`ADD COLUMN` 式迁移
**最危险的失败模式是静默的** —— 它可能丢行、漏建索引，或者**回填写错**。
回填错在这里尤其隐蔽：告警一条不少、接口也不报错，只是"别人的告警悄悄出现在我
的告警中心里"，而这类问题要等到两个账号真的同时在用才会被人发现。

不依赖真实数据库、不依赖 torch：全程在 tmp_path 里建一个"旧库"。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"


def _load_tool():
    """按路径加载 tools/ 下的脚本（它不在 backend 包内，import 不到）。"""
    spec = importlib.util.spec_from_file_location(
        "migrate_add_ownership", TOOLS_DIR / "migrate_add_ownership.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _load_tool()


#: 迁移前的库：users / tasks / alerts / plots 都在，但两张表都没有 owner_id
LEGACY_DDL = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64));
CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER, plot_id INTEGER);
CREATE TABLE plots (id INTEGER PRIMARY KEY, name VARCHAR(128), crop VARCHAR(64));
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    plot_id INTEGER,
    task_id INTEGER,
    level VARCHAR(16),
    message TEXT
);
"""


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(LEGACY_DDL)
        con.executemany(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            [(1, "farmer"), (2, "neighbour")],
        )
        con.executemany(
            "INSERT INTO tasks (id, user_id, plot_id) VALUES (?, ?, ?)",
            [
                (1, 1, 1),      # farmer 的
                (2, 2, None),   # neighbour 的
                (3, None, None),  # 本机抓拍：无主 = 公共
            ],
        )
        con.execute("INSERT INTO plots (id, name, crop) VALUES (1, '东头', '水稻')")
        con.executemany(
            "INSERT INTO alerts (id, plot_id, task_id, level, message) VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, 1, "warn", "farmer 的告警"),
                (2, None, 2, "warn", "neighbour 的告警"),
                (3, None, 3, "info", "无主任务的告警 —— 回填后仍是 NULL"),
                (4, None, None, "info", "更早的、本来就没有任务的告警 —— 仍是 NULL"),
            ],
        )
        con.commit()
    finally:
        con.close()
    return db


def columns(db: Path, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def row_count(db: Path, table: str) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def alert_owners(db: Path) -> dict[int, int | None]:
    con = sqlite3.connect(db)
    try:
        return {row[0]: row[1] for row in con.execute("SELECT id, owner_id FROM alerts")}
    finally:
        con.close()


def index_names(db: Path, table: str) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
            )
        }
    finally:
        con.close()


def test_plan_sees_both_missing_columns(tool, legacy_db: Path):
    plans = {item["table"]: item for item in tool.plan(legacy_db)}

    assert [name for name, _ in plans["alerts"]["pending"]] == ["owner_id"]
    assert [name for name, _ in plans["plots"]["pending"]] == ["owner_id"]
    assert plans["alerts"]["rows"] == 4
    assert plans["plots"]["rows"] == 1


def test_apply_adds_columns_indexes_and_backfills_alert_owner(tool, legacy_db: Path):
    """核心：列、索引、**回填**三件事一次做对。

    回填的期望写成完整的字典而不是子集：四条告警分别覆盖
    "有主的任务" / "另一个账号的任务" / "无主任务" / "没有任务"，
    少断言任何一种，都可能放过"用 plot_id 回填"或"无主也塞个默认账号"这类错误。
    """
    tool.apply(legacy_db, tool.plan(legacy_db))

    assert "owner_id" in columns(legacy_db, "alerts")
    assert "owner_id" in columns(legacy_db, "plots")
    assert "ix_alerts_owner" in index_names(legacy_db, "alerts")
    assert "ix_plots_owner" in index_names(legacy_db, "plots")

    # 一条都不能少
    assert row_count(legacy_db, "alerts") == 4
    assert row_count(legacy_db, "plots") == 1

    assert alert_owners(legacy_db) == {
        1: 1,     # 从 task 1 的 user_id 抄过来
        2: 2,     # 从 task 2 的 user_id 抄过来
        3: None,  # task 3 无主 → 告警也保持无主（= 公共）
        4: None,  # 没有任务 → 保持无主
    }
    # 历史地块一律留 NULL（= 公共）—— 也就是升级前的行为，不丢数据
    con = sqlite3.connect(legacy_db)
    try:
        assert con.execute("SELECT owner_id FROM plots").fetchone()[0] is None
    finally:
        con.close()


def test_second_run_is_a_noop_and_does_not_rewrite_owner(tool, legacy_db: Path):
    """幂等：第二次不该再补列，也不该把手工改过的归属又按任务覆盖回去。"""
    tool.apply(legacy_db, tool.plan(legacy_db))

    # 模拟"迁移之后有人手工调整过归属"
    con = sqlite3.connect(legacy_db)
    try:
        con.execute("UPDATE alerts SET owner_id = 2 WHERE id = 1")
        con.commit()
    finally:
        con.close()

    plans = tool.plan(legacy_db)
    assert all(item["pending"] == [] for item in plans)
    tool.apply(legacy_db, plans)  # 再跑一次不该炸

    assert alert_owners(legacy_db)[1] == 2, "没有新加列时不该执行回填"
    assert row_count(legacy_db, "alerts") == 4


def test_apply_backs_up_before_touching(tool, legacy_db: Path):
    """动数据之前必须先留一份带时间戳的备份 —— 这是唯一的退路。"""
    tool.apply(legacy_db, tool.plan(legacy_db))

    backups = list(legacy_db.parent.glob(f"{legacy_db.name}.bak-*"))
    assert len(backups) == 1
    # 备份里还应该是迁移**前**的样子
    assert "owner_id" not in columns(backups[0], "alerts")
    assert row_count(backups[0], "alerts") == 4


def test_blank_database_needs_no_alter(tool, tmp_path: Path):
    """全新的库：表还不存在，create_all 会连新列一起建好，不该走 ALTER 分支。"""
    db = tmp_path / "blank.db"
    sqlite3.connect(db).close()

    plans = tool.plan(db)
    assert all(item["missing_table"] for item in plans)
    assert all(item["pending"] == [] for item in plans)
