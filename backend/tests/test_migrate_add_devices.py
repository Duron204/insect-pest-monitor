# -*- coding: utf-8 -*-
"""`tools/migrate_add_devices.py` 的回归测试。

为什么值得给一个迁移脚本写测试：整表重建式的迁移出错会立刻炸，
而 `ALTER TABLE ADD COLUMN` 式的迁移**最危险的失败模式是静默的** ——
它可能删掉行、可能漏建索引、也可能第二次跑就把库搞坏。
这里就是把这三件事钉住。

不依赖真实数据库、不依赖 torch：全程在一个 tmp_path 里建一个"旧库"。
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
        "migrate_add_devices", TOOLS_DIR / "migrate_add_devices.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _load_tool()


#: 迁移前的老库：tasks 存在但**没有** device_id
LEGACY_DDL = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    mode VARCHAR(16),
    user_id INTEGER,
    source_name VARCHAR(512)
);
"""


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(LEGACY_DDL)
        con.executemany(
            "INSERT INTO tasks (id, mode, user_id, source_name) VALUES (?, ?, ?, ?)",
            [
                (1, "image", 1, "a.jpg"),
                (2, "local", None, "本机摄像头（opencv）"),
                (3, "video", 2, "b.mp4"),
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


def test_plan_sees_the_missing_column(tool, legacy_db: Path):
    plans = tool.plan(legacy_db)

    # tasks 要补列；devices 在这台"旧库"里压根不存在 → 由 create_all 连列一起建
    assert [p["table"] for p in plans] == ["tasks", "devices"]
    item = plans[0]
    assert item["missing_table"] is False
    assert [name for name, _ in item["pending"]] == ["device_id"]
    assert item["rows"] == 3
    assert plans[1]["missing_table"] is True


#: 已经**启动过一次**、devices 表已经建出来但没有 `pairing_secret_hash` 的库。
#: 这是升级时真正会遇到的形状：`create_all` 只建缺的表，不给已有的表补列。
DEVICES_WITHOUT_SECRET_DDL = """
CREATE TABLE devices (
    id INTEGER PRIMARY KEY,
    hardware_id VARCHAR(64) NOT NULL,
    name VARCHAR(96),
    status VARCHAR(16),
    pending_token_plain VARCHAR(128)
);
"""


def test_existing_devices_table_gets_the_credential_columns(tool, tmp_path: Path):
    """已经建过 `devices` 表的库要能被补上两列凭证哈希。

    漏掉这条 DDL 的症状很隐蔽：库能开、设备页能开、配对码也能显示，
    只有盒子轮询时永远拿不到令牌（写入时撞上"表里没有这一列"）。
    所以这里既断言计划里有它，也真的跑一遍 apply 看列有没有落地。

    两列都要，而且是**顺序敏感**的：先 `pairing_secret_hash` 再它绑定时的快照
    `pending_token_secret_hash`。生产里只补上前一列（或只补后一列）都会留下
    一个能开、但一绑定就 500 的库 —— 所以断言写成列表相等而不是子集。
    """
    db = tmp_path / "with-devices.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(DEVICES_WITHOUT_SECRET_DDL)
        # 表里有数据也要能加列 —— NULL 默认值本来就是给这种情况准备的
        con.execute(
            "INSERT INTO devices (id, hardware_id, name, status) VALUES (1, 'pi-x', '东头', 'active')"
        )
        con.commit()
    finally:
        con.close()

    plans = tool.plan(db)
    by_table = {item["table"]: item for item in plans}
    assert [name for name, _ in by_table["devices"]["pending"]] == [
        "pairing_secret_hash",
        "pending_token_secret_hash",
    ]

    tool.apply(db, plans)
    assert "pairing_secret_hash" in columns(db, "devices")
    assert "pending_token_secret_hash" in columns(db, "devices")
    assert row_count(db, "devices") == 1, "补列不能丢行"


def test_apply_adds_column_creates_tables_and_preserves_rows(tool, legacy_db: Path):
    plans = tool.plan(legacy_db)
    tool.apply(legacy_db, plans)

    # 唯一的那句 DDL 生效了
    assert "device_id" in columns(legacy_db, "tasks")
    # 三行数据一行不能少
    assert row_count(legacy_db, "tasks") == 3
    # 两张新表由 create_all 建出来了，且 device_id 的外键父表确实在
    assert "devices" in columns_of_tables(legacy_db)
    assert "device_ingest" in columns_of_tables(legacy_db)
    # 索引也建了
    assert "ix_tasks_device_id" in index_names(legacy_db, "tasks")


def columns_of_tables(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
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


def test_second_run_is_a_noop(tool, legacy_db: Path):
    """幂等：再来一次不应该报错，也不应该改动任何东西。"""
    tool.apply(legacy_db, tool.plan(legacy_db))
    after_first = columns(legacy_db, "tasks")

    plans = tool.plan(legacy_db)
    assert plans[0]["pending"] == []          # 已经识别出"没事可做"
    tool.apply(legacy_db, plans)              # 再跑一次也不该炸

    assert columns(legacy_db, "tasks") == after_first
    assert row_count(legacy_db, "tasks") == 3


def test_blank_database_needs_no_alter(tool, tmp_path: Path):
    """全新的库：tasks 还不存在，create_all 会连新列一起建好，不该走 ALTER 分支。"""
    db = tmp_path / "blank.db"
    sqlite3.connect(db).close()

    plans = tool.plan(db)
    assert plans[0]["missing_table"] is True
    assert plans[0]["pending"] == []


def test_apply_backs_up_before_touching(tool, legacy_db: Path):
    """动数据之前必须先留一份带时间戳的备份 —— 这是唯一的退路。"""
    tool.apply(legacy_db, tool.plan(legacy_db))

    backups = list(legacy_db.parent.glob(f"{legacy_db.name}.bak-*"))
    assert len(backups) == 1
    # 备份里还应该是迁移**前**的样子
    assert "device_id" not in columns(backups[0], "tasks")
    assert row_count(backups[0], "tasks") == 3
