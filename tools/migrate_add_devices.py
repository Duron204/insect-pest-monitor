# -*- coding: utf-8 -*-
"""为"账号绑定树莓派设备"补齐 schema。

要加的东西只有两类，而**其中一类根本不需要迁移**：

  1. **两张新表 `devices` / `device_ingest`** —— 什么都不用做。
     `create_app` 启动时会调 `db.create_all()`（→ `Base.metadata.create_all`），
     它建缺失的表、跳过已存在的表，所以这两张表下次启动就自动有了。
     想不等下次启动就先建好，用 `--create-tables`。
  2. **`tasks.device_id`** 与 **`devices` 上的两个凭证哈希列**
     （`pairing_secret_hash`、`pending_token_secret_hash`）—— 需要 DDL，
     但都只是一句 `ALTER TABLE ... ADD COLUMN`。后面两条只在**已经启动过一次**、
     `devices` 表已经建出来的库上才需要（`create_all` 只建缺的表，不会给已有的表补列）。

**为什么不用像 `migrate_drop_legacy_columns.py` 那样整表重建**：
重建是为了删列（以及摆脱约束）。而 SQLite 的 `ALTER TABLE ADD COLUMN` 加一个
可空、无默认值的列是原生支持、且**不碰任何既有数据**的。重建唯一的任务表
风险远大于收益，没必要。

顺带说明为什么幂等台账是**单独一张表**而不是给 tasks 加唯一列：
`ALTER TABLE ADD COLUMN` 带不了 UNIQUE 约束，那才会逼出整表重建。

**安全措施**（与既有的迁移工具一致）：
  * 默认**干跑**，只报告将要做什么；必须显式 `--apply`
  * apply 前自动备份数据库文件（带时间戳），并打印备份路径
  * 全程在一个事务里，任一步失败即回滚
  * 提交后校验：列已存在、行数未变、`integrity_check`、`foreign_key_check`

用法：
    python tools/migrate_add_devices.py                    # 干跑
    python tools/migrate_add_devices.py --apply            # 备份并执行
    python tools/migrate_add_devices.py --apply --create-tables   # 顺带把新表建出来
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import load_settings  # noqa: E402

#: 新增的表：由 create_all 建，不需要迁移
NEW_TABLES: tuple[str, ...] = ("devices", "device_ingest")

#: 要在既有表上补的列：表名 -> [(列名, 列类型)]
ADD_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "tasks": [("device_id", "INTEGER REFERENCES devices(id)")],
    # 配对握手凭证的哈希，以及它在"绑定那一刻"的快照。两者都**刻意不建索引**：
    # 从不单独查询，总是先按 hardware_id 取到行、再拿它做一次常量时间比对。
    # 快照那一列的存在理由见 models.py 里它的注释（防"绑定后再发一次 pair/start
    # 顶替领令牌的人"）。
    "devices": [
        ("pairing_secret_hash", "VARCHAR(64)"),
        ("pending_token_secret_hash", "VARCHAR(64)"),
    ],
}

#: 补列之后要建的索引：表名 -> [(索引名, 索引列)]
ADD_INDEXES: dict[str, list[tuple[str, str]]] = {
    "tasks": [("ix_tasks_device_id", "device_id")],
}


def resolve_db_path() -> Path:
    settings = load_settings()
    url = settings.database.url
    if not url.startswith("sqlite:///"):
        raise SystemExit(f"只处理 sqlite，当前是：{url}")
    raw = url[len("sqlite:///") :]
    path = Path(raw)
    if not path.is_absolute():
        path = (settings.project_root / path).resolve()
    return path


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def plan(db_path: Path) -> list[dict]:
    """检查每个待补的列是否已经存在，产出计划。"""
    con = sqlite3.connect(db_path)
    try:
        plans: list[dict] = []
        for table, columns in ADD_COLUMNS.items():
            if not table_exists(con, table):
                # 全新的库：create_all 会把整张表连同新列一起建好，无需 ALTER
                plans.append({"table": table, "missing_table": True, "pending": [], "rows": 0})
                continue
            present = table_columns(con, table)
            pending = [(name, ddl) for name, ddl in columns if name not in present]
            plans.append(
                {
                    "table": table,
                    "missing_table": False,
                    "present_columns": present,
                    "pending": pending,
                    "rows": con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                }
            )
        return plans
    finally:
        con.close()


def report(plans: list[dict], db_path: Path) -> None:
    print(f"数据库：{db_path}")
    print(f"\n新表（由 create_all 自动创建，无需迁移）：{list(NEW_TABLES)}")
    for item in plans:
        table = item["table"]
        if item["missing_table"]:
            print(f"\n[{table}]  表还不存在 —— create_all 会连同新列一起建好，无需 ALTER")
            continue
        print(f"\n[{table}]  当前 {item['rows']} 行")
        if item["pending"]:
            for name, ddl in item["pending"]:
                print(f"  将新增列：{name} {ddl}")
        else:
            print("  将新增列：（无，已是最新）")


def ensure_new_tables(db_path: Path) -> None:
    """用当前模型把缺失的表建出来（幂等）。

    为什么在 ALTER 之前做：`tasks.device_id` 的 REFERENCES 指向 `devices`，
    先把父表建出来，外键引用就不会悬空。
    """
    from sqlalchemy import create_engine

    from app.db import Base  # noqa: PLC0415
    from app import models  # noqa: F401, PLC0415  必须 import 才会注册到 metadata

    engine = create_engine("sqlite:///" + db_path.as_posix())
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()


def apply(db_path: Path, plans: list[dict]) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    shutil.copy2(db_path, backup)
    print(f"\n已备份：{backup}（{backup.stat().st_size / 1024:.0f} KB）")

    ensure_new_tables(db_path)

    todo = [item for item in plans if not item["missing_table"] and item["pending"]]
    if not todo:
        return

    con = sqlite3.connect(db_path)
    con.isolation_level = None
    try:
        # 必须在 BEGIN 之外设置：SQLite 在事务中会忽略 foreign_keys 开关。
        # 关掉是为了不受 "父表/子表顺序" 影响，加完再开回来（连接关闭即失效）。
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN")
        for item in todo:
            table = item["table"]
            for name, ddl in item["pending"]:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            for index_name, column in ADD_INDEXES.get(table, []):
                con.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})"
                )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def verify(db_path: Path, plans: list[dict]) -> None:
    con = sqlite3.connect(db_path)
    try:
        print("\n校验：")
        for item in plans:
            table = item["table"]
            if item["missing_table"]:
                ok = table_exists(con, table)
                print(f"  [{table}] 表已创建：{'[OK]' if ok else '!! 仍然不存在'}")
                continue
            columns = table_columns(con, table)
            rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            left = [name for name, _ in item["pending"] if name not in columns]
            ok_rows = "[OK]" if rows == item["rows"] else "!! 行数不符"
            print(
                f"  [{table}] 行数 {item['rows']} -> {rows} {ok_rows}；"
                f"仍缺的列：{left or '无 [OK]'}"
            )
        print(f"  integrity_check: {con.execute('PRAGMA integrity_check').fetchone()[0]}")
        dangling = con.execute("PRAGMA foreign_key_check").fetchall()
        print(f"  foreign_key_check: {'无悬空引用 [OK]' if not dangling else f'!! {dangling}'}")
    finally:
        con.close()


def main() -> int:
    # Windows 控制台默认可能是 GBK，输出里一旦有它编不了的字符就会在校验打印那步抛
    # UnicodeEncodeError —— 数据其实已经迁移完成，但用户会以为失败了。
    # 沿用既有迁移工具的两道保险：ASCII 标记 + 输出永不因编码中断。
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="为设备绑定功能补齐 schema")
    parser.add_argument("--apply", action="store_true", help="备份并真正执行（默认只干跑）")
    parser.add_argument(
        "--create-tables",
        action="store_true",
        help="顺带把 devices / device_ingest 两张新表建出来（否则等下次启动自动建）",
    )
    args = parser.parse_args()

    db_path = resolve_db_path()
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}")

    plans = plan(db_path)
    report(plans, db_path)

    pending = [item for item in plans if not item["missing_table"] and item["pending"]]
    if not pending and not args.create_tables:
        print("\n没有需要新增的列，无需操作。")
        print("（想现在就建出新表，加 --create-tables。）")
        return 0

    if not args.apply:
        print("\n（干跑结束，未改动任何数据。加 --apply 执行。）")
        return 0

    apply(db_path, plans)
    verify(db_path, plans)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
