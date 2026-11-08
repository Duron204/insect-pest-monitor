# -*- coding: utf-8 -*-
"""删除 tasks / alerts 里的历史遗留空列。

**为什么会有这些列**：它们属于已经整体移除的"设备接入层"
（ESP32 上报 → `device_id`/`ingest_key`/`captured_at`/`device_seq`）。
移除那层时我没有删列，因为在当时 SQLite 无法直接 DROP 一个带 UNIQUE 约束的列
（`ingest_key` 有 UNIQUE，SQLite 会拒绝），而人工重建唯一的任务表风险大于收益。
现在把它们清掉，顺带解决两个遗留问题：

  * `alerts.device_id` 上有一条**指向已删除的 `devices` 表的外键**（悬空引用）
  * `tasks` 上有一条 `ix_tasks_device_id` 索引，索引的列马上就不存在了

**做法**（SQLite 官方推荐的"重建表"流程，一个事务内完成）：
  1. 用当前模型（SQLAlchemy metadata）在一个临时库里生成"正确"的建表语句
  2. `CREATE TABLE <t>_new` → `INSERT ... SELECT` 公共列 → `DROP TABLE <t>` → `RENAME`
  3. 按当前模型重建索引
  4. 提交后校验：行数一致、`integrity_check`、`foreign_key_check`、列已消失

**安全措施**：
  * 默认**干跑**，只报告将要做什么；必须显式 `--apply`
  * apply 前自动备份数据库文件（带时间戳），并打印备份路径
  * 若发现"即将丢失的、与遗留列无关的索引"则中止（防止模型与库不一致时误删）
  * 全程在一个事务里，任一步失败即回滚

用法：
    python tools/migrate_drop_legacy_columns.py           # 干跑
    python tools/migrate_drop_legacy_columns.py --apply   # 备份并执行
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import load_settings  # noqa: E402
from app.db import Base  # noqa: E402

# 必须 import 模型模块：表定义是在类声明时注册到 Base.metadata 上的，
# 只 import Base 的话 metadata 是空的（create_all 会建出空库）。
from app import models  # noqa: E402, F401

#: 表名 -> 该表里要删除的遗留列
#:
#: ⚠️ 这里的每一列都必须**在"当前模型里已经不存在"**。表是按当前模型重建的，
#: 所以万一某列其实又回到了模型里，重建之后列还在、**值却全是 NULL** —— 一次
#: 静默的整列数据清空，而脚本还会报"行数一致、校验通过"。
#:
#: `tasks.device_id` 就是这么踩到的：设备接入把这一列接了回来（models.py 的
#: `Task.device_id`），名单里却还留着它，于是跑一次就抹掉全部历史任务的设备归属。
#: `plan()` 里现在有一道反向检查专门拦这种情况，但往这里加列时仍要人工确认一遍。
LEGACY_COLUMNS: dict[str, tuple[str, ...]] = {
    "tasks": ("ingest_key", "captured_at", "device_seq"),
    "alerts": ("device_id",),
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


def fresh_schema(table: str) -> tuple[str, list[str]]:
    """用当前模型在一个临时库里生成该表的 DDL 与索引 DDL。"""
    from sqlalchemy import create_engine

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{(Path(tmp) / 'schema.db').as_posix()}")
        Base.metadata.create_all(bind=engine)
        engine.dispose()
        con = sqlite3.connect(Path(tmp) / "schema.db")
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None or not row[0]:
                raise SystemExit(f"当前模型里没有表 {table}")
            ddl = " ".join(row[0].split())
            indexes = [
                " ".join(r[0].split())
                for r in con.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                    (table,),
                )
            ]
        finally:
            con.close()
    return ddl, indexes


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


def existing_indexes(con: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        row[0]: " ".join(row[1].split())
        for row in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        )
    }


def plan(db_path: Path) -> list[dict]:
    """对比当前库与当前模型，产出每个表的迁移计划。"""
    con = sqlite3.connect(db_path)
    try:
        plans: list[dict] = []
        for table, legacy in LEGACY_COLUMNS.items():
            columns = table_columns(con, table)
            present = [c for c in legacy if c in columns]
            ddl, index_ddl = fresh_schema(table)
            keep = [c for c in columns if c not in legacy]
            missing_in_model = [c for c in keep if c not in ddl]
            if missing_in_model:
                raise SystemExit(
                    f"{table}: 库里有当前模型没有的列 {missing_in_model}，请先处理模型不一致，已中止"
                )
            # 反向检查 —— 上面查的是"库里有、模型没有"，这里查"列为遗留、模型却还有"。
            # 少了这一道，名单一旦过期就会**静默清空那一列的全部数据**（见
            # LEGACY_COLUMNS 的注释）。宁可在这里吵，也不要在人家的库上删数据。
            still_in_model = [c for c in legacy if c in ddl]
            if still_in_model:
                raise SystemExit(
                    f"{table}: {still_in_model} 在 LEGACY_COLUMNS 里，但**当前模型里仍然存在**。"
                    f"继续执行会把这几个列搬不过去，重建后值全是 NULL（整列数据丢失）。"
                    f"请先从 LEGACY_COLUMNS 里删掉它们，已中止"
                )
            old_indexes = existing_indexes(con, table)
            new_names = {_index_name(sql) for sql in index_ddl}
            # 只允许丢失"建在遗留列上的"索引
            losing = [
                name
                for name, sql in old_indexes.items()
                if name not in new_names and not any(col in sql for col in legacy)
            ]
            plans.append(
                {
                    "table": table,
                    "present": present,
                    "drop": [c for c in legacy if c in columns],
                    "keep": keep,
                    "rows": con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    "ddl": ddl,
                    "indexes": index_ddl,
                    "losing_indexes": losing,
                    "old_indexes": old_indexes,
                }
            )
        return plans
    finally:
        con.close()


def _index_name(ddl: str) -> str:
    # CREATE [UNIQUE] INDEX <name> ON ...
    parts = ddl.split()
    try:
        return parts[parts.index("INDEX") + 1]
    except (ValueError, IndexError):
        return ""


def report(plans: list[dict], db_path: Path) -> None:
    print(f"数据库：{db_path}")
    for item in plans:
        print(f"\n[{item['table']}]  当前 {item['rows']} 行")
        print(f"  将删除列：{item['drop'] or '（无）'}")
        print(f"  保留列：{len(item['keep'])} 个")
        print(f"  重建索引：{[_index_name(s) for s in item['indexes']]}")
        if item["losing_indexes"]:
            print(f"  !! 会丢失与遗留列无关的索引：{item['losing_indexes']}")


def apply(db_path: Path, plans: list[dict]) -> None:
    for item in plans:
        if item["losing_indexes"]:
            raise SystemExit(f"{item['table']}: 会丢失无关索引，已中止：{item['losing_indexes']}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    shutil.copy2(db_path, backup)
    print(f"\n已备份：{backup}（{backup.stat().st_size / 1024:.0f} KB）")

    con = sqlite3.connect(db_path)
    con.isolation_level = None
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN")
        for item in plans:
            table = item["table"]
            cols = ", ".join(item["keep"])
            # 用临时表名建新表，再把数据搬过去
            new_ddl = item["ddl"].replace(f"CREATE TABLE {table} ", f"CREATE TABLE {table}_new ", 1)
            con.execute(new_ddl)
            con.execute(f"INSERT INTO {table}_new ({cols}) SELECT {cols} FROM {table}")
            moved = con.execute(f"SELECT COUNT(*) FROM {table}_new").fetchone()[0]
            if moved != item["rows"]:
                raise SystemExit(f"{table}: 搬运后行数 {moved} != 原 {item['rows']}，已回滚")
            con.execute(f"DROP TABLE {table}")
            con.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            for ddl in item["indexes"]:
                con.execute(ddl)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    # ---- 校验 ----
    con = sqlite3.connect(db_path)
    try:
        print("\n校验：")
        for item in plans:
            table = item["table"]
            columns = table_columns(con, table)
            rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            left = [c for c in item["drop"] if c in columns]
            ok_rows = "[OK]" if rows == item["rows"] else "!! 行数不符"
            print(
                f"  [{table}] 行数 {item['rows']} -> {rows} {ok_rows}；"
                f"残留遗留列：{left or '无 [OK]'}"
            )
        print(f"  integrity_check: {con.execute('PRAGMA integrity_check').fetchone()[0]}")
        dangling = con.execute("PRAGMA foreign_key_check").fetchall()
        print(f"  foreign_key_check: {'无悬空引用 [OK]' if not dangling else f'!! {dangling}'}")
    finally:
        con.close()


def main() -> int:
    # Windows 控制台默认可能是 GBK，输出里一旦有它编不了的字符（例如 ✓）就会
    # 在**校验打印**这一步抛 UnicodeEncodeError —— 数据其实已经迁移完成，
    # 但用户会以为失败了。这里做两道保险：改用 ASCII 标记 + 输出永不因编码中断。
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="删除 tasks/alerts 的遗留空列")
    parser.add_argument("--apply", action="store_true", help="备份并真正执行（默认只干跑）")
    args = parser.parse_args()

    db_path = resolve_db_path()
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}")

    plans = plan(db_path)
    report(plans, db_path)

    if not any(item["drop"] for item in plans):
        print("\n没有需要删除的遗留列，无需操作。")
        return 0

    if not args.apply:
        print("\n（干跑结束，未改动任何数据。加 --apply 执行。）")
        return 0

    apply(db_path, plans)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
