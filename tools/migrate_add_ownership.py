# -*- coding: utf-8 -*-
"""为"按账号分开数据"补齐 schema。

要加的东西只有一列两处，外加一次回填：

  1. **`alerts.owner_id`** —— 告警的归属。从触发它的任务抄过来。
  2. **`plots.owner_id`** —— 地块的归属。历史地块**一律留 NULL（= 公共）**，
     不做任何回填：那是它今天的行为，升级后一个人都不会丢数据。
  3. **回填 `alerts.owner_id`** —— 从 `tasks.user_id` 抄一遍。不回填的话，
     升级后所有历史告警都会变成"无主 = 公共"，也就是**所有人的告警中心里都能看到
     别人以前的告警**。这不是灾难，但明显不是想要的。

为什么不用整表重建：`ALTER TABLE ADD COLUMN` 加一个可空、无默认值的列是 SQLite
原生支持的操作，**不碰任何既有数据**。重建唯一的风险远大于收益。
（`Plot.name` 的全场唯一约束刻意保留 —— 见 models.py 里 `Plot.owner_id` 的注释。）

**安全措施**（与既有的 `migrate_add_devices.py` / `migrate_drop_legacy_columns.py` 一致）：
  * 默认**干跑**，只报告将要做什么；必须显式 `--apply`
  * apply 前自动备份数据库文件（带时间戳），并打印备份路径
  * 全程在一个事务里，任一步失败即回滚
  * 提交后校验：列已存在、行数未变、回填到位、`integrity_check`、`foreign_key_check`

用法：
    python tools/migrate_add_ownership.py            # 干跑
    python tools/migrate_add_ownership.py --apply    # 备份并执行
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

#: 要在既有表上补的列：表名 -> [(列名, 列类型)]
ADD_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "alerts": [("owner_id", "INTEGER REFERENCES users(id)")],
    "plots": [("owner_id", "INTEGER REFERENCES users(id)")],
}

#: 补列之后要建的索引：表名 -> [(索引名, 索引列)]
ADD_INDEXES: dict[str, list[tuple[str, str]]] = {
    "alerts": [("ix_alerts_owner", "owner_id")],
    "plots": [("ix_plots_owner", "owner_id")],
}

#: 补列之后的一次性回填：表名 -> SQL。只在**本次真的加了那一列**时执行 ——
#: 否则重复运行会把别人后来手工改过的归属又按任务覆盖回去。
#: 相关子查询：告警的 owner 就是触发它的任务的 user_id（可能是 NULL，那就还是公共）。
BACKFILL: dict[str, str] = {
    "alerts": """
        UPDATE alerts
           SET owner_id = (SELECT t.user_id FROM tasks t WHERE t.id = alerts.task_id)
         WHERE owner_id IS NULL
    """,
}

#: 回填之后应当为 0 的行数（有任务归属、却没抄到告警上 = 回填漏了）
BACKFILL_CHECK: dict[str, str] = {
    "alerts": """
        SELECT COUNT(*) FROM alerts a
          JOIN tasks t ON t.id = a.task_id
         WHERE a.owner_id IS NULL AND t.user_id IS NOT NULL
    """,
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
    for item in plans:
        table = item["table"]
        if item["missing_table"]:
            print(f"\n[{table}]  表还不存在 —— create_all 会连同新列一起建好，无需 ALTER")
            continue
        print(f"\n[{table}]  当前 {item['rows']} 行")
        if item["pending"]:
            for name, ddl in item["pending"]:
                print(f"  将新增列：{name} {ddl}")
            if table in BACKFILL:
                print(f"  随后回填：{BACKFILL[table].strip().splitlines()[0]} ...")
        else:
            print("  将新增列：（无，已是最新）")


def apply(db_path: Path, plans: list[dict]) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    shutil.copy2(db_path, backup)
    print(f"\n已备份：{backup}（{backup.stat().st_size / 1024:.0f} KB）")

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
            added = {name for name, _ in item["pending"]}
            for name, ddl in item["pending"]:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            for index_name, column in ADD_INDEXES.get(table, []):
                con.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})"
                )
            # 只在本次真加了 owner_id 时回填（见 BACKFILL 的注释）
            if "owner_id" in added and table in BACKFILL:
                con.execute(BACKFILL[table])
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
            check_sql = BACKFILL_CHECK.get(table)
            if check_sql is not None:
                missed = con.execute(check_sql).fetchone()[0]
                print(
                    f"  [{table}] 回填后漏掉的归属：{missed} 行 "
                    f"{'[OK]' if missed == 0 else '!! 回填未完成'}"
                )
        print(f"  integrity_check: {con.execute('PRAGMA integrity_check').fetchone()[0]}")
        dangling = con.execute("PRAGMA foreign_key_check").fetchall()
        print(f"  foreign_key_check: {'无悬空引用 [OK]' if not dangling else f'!! {dangling}'}")
    finally:
        con.close()


def main() -> int:
    # Windows 控制台默认可能是 GBK，输出里一旦有它编不了的字符就会在校验打印那步抛
    # UnicodeEncodeError —— 数据其实已经迁移完成，但用户会以为失败了。
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="为按账号分开数据补齐 schema")
    parser.add_argument("--apply", action="store_true", help="备份并真正执行（默认只干跑）")
    args = parser.parse_args()

    db_path = resolve_db_path()
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}")

    plans = plan(db_path)
    report(plans, db_path)

    pending = [item for item in plans if not item["missing_table"] and item["pending"]]
    if not pending:
        print("\n没有需要新增的列，无需操作。")
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
