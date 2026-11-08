# -*- coding: utf-8 -*-
"""`tools/migrate_drop_legacy_columns.py` 的反向守卫。

这个脚本是**整表重建**式的迁移：它按 `LEGACY_COLUMNS` 决定"哪些列不要了"，
然后新建表、把要留的列搬过去。所以名单写错一个字的后果不是报错，而是
**那一列的值全变 NULL，脚本还报告成功**。

`tasks.device_id` 正是这么踩到的：设备接入把这一列接了回来（`Task.device_id`
在模型里确实存在），名单里却还留着它 —— 跑一次就抹掉全部历史任务的设备归属。

`plan()` 里那道反向守卫就是为这种情况加的。这个文件把两件事钉住：
守卫本身有效，以及**出厂的名单现在是干净的**。
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
        "migrate_drop_legacy_columns", TOOLS_DIR / "migrate_drop_legacy_columns.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _load_tool()


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    """一个"迁移前"的库：模型的全套列都在，另外还带着那三个遗留列。

    直接用当前模型建表再 `ALTER TABLE ADD COLUMN` 补上遗留列 ——
    这样 `plan()` 的"库里有、模型没有"那道检查（`missing_in_model`）
    不会被误触发，测的才是我们要测的那道反向守卫。
    """
    from sqlalchemy import create_engine

    from app.db import Base
    import app.models  # noqa: F401  确保所有模型都注册进 Base.metadata

    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()

    con = sqlite3.connect(db)
    try:
        con.executescript(
            "ALTER TABLE tasks ADD COLUMN ingest_key VARCHAR(64);"
            "ALTER TABLE tasks ADD COLUMN captured_at DATETIME;"
            "ALTER TABLE tasks ADD COLUMN device_seq INTEGER;"
            "ALTER TABLE alerts ADD COLUMN device_id INTEGER;"
        )
        con.commit()
    finally:
        con.close()
    return db


def test_the_shipped_list_does_not_include_a_live_column(tool, legacy_db: Path):
    """⭐ 出厂名单必须干净 —— 一条都没撞上模型里仍然存在的列。

    `tasks.device_id` 曾经就在这份名单里。它是模型里**真实存在**的列，
    被列进 `LEGACY_COLUMNS` 就意味着"重建任务表时不要搬它"，
    于是那一列的历史值全部变成 NULL，而脚本会报告成功。

    这条用例把"名单必须干净"钉在**配置**上，而不只是钉在守卫的代码上：
    谁把 `device_id` 再加回去，这里立刻红。
    """
    plans = tool.plan(legacy_db)

    assert [p["table"] for p in plans] == ["tasks", "alerts"]
    # 只该丢那三个真的已经不在模型里的列
    assert sorted(plans[0]["drop"]) == ["captured_at", "device_seq", "ingest_key"]


def test_plan_refuses_when_a_legacy_column_is_still_in_the_model(tool, legacy_db, monkeypatch):
    """⭐ 守卫本身：名单里出现"模型里还在"的列，必须当场中止，绝不静默重建。

    这里手工把 `device_id` 塞回名单 —— 也就是**复现那个真实发生过的 bug** ——
    再确认 `plan()` 拒绝干活，而且错误信息点得出来是哪一列。
    """
    monkeypatch.setitem(
        tool.LEGACY_COLUMNS, "tasks", ("ingest_key", "captured_at", "device_seq", "device_id")
    )

    with pytest.raises(SystemExit) as excinfo:
        tool.plan(legacy_db)

    message = str(excinfo.value)
    assert "device_id" in message, "得说清楚是哪一列闯的祸"
    assert "仍然存在" in message
    assert "已中止" in message


def test_plan_is_idempotent_after_apply(tool, legacy_db: Path):
    """跑完一次之后必须没事可做 —— 否则 `--apply` 每次都会重建一遍表。"""
    tool.apply(legacy_db, tool.plan(legacy_db))

    con = sqlite3.connect(legacy_db)
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(tasks)")]
    finally:
        con.close()

    assert "device_id" in columns, "模型里存在的列必须留下"
    assert not {"ingest_key", "captured_at", "device_seq"} & set(columns)

    # 第二次：遗留列已经没了，计划里应当没有可丢的东西
    assert tool.plan(legacy_db)[0]["drop"] == []
