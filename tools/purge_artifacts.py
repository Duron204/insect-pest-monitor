# -*- coding: utf-8 -*-
"""按 `storage.retention_days` 清理过期产物与上传文件。

用法（项目根目录、用后端的解释器）：

    python tools/purge_artifacts.py            # 真正执行
    python tools/purge_artifacts.py --dry-run  # 只报告会清理什么
    python tools/purge_artifacts.py --days 7   # 临时改保留天数（不改配置文件）

现场用 `deploy/insect-maintenance.timer` 每天跑一次；
后端启动时也会自动跑一次作为兜底。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import load_settings  # noqa: E402
from app.db import init_engine, session_factory  # noqa: E402
from app.services.maintenance import preview_purge, purge_artifacts  # noqa: E402


def _mb(value: float) -> str:
    return f"{value / 1e6:.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description="清理过期产物与上传文件")
    parser.add_argument("--days", type=int, help="覆盖配置里的 retention_days")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不删除")
    args = parser.parse_args()

    settings = load_settings()
    if args.days is not None:
        settings = replace(settings, storage=replace(settings.storage, retention_days=args.days))

    init_engine(settings.resolved_database_url)
    session = session_factory()()
    try:
        if args.dry_run:
            info = preview_purge(settings, session)
            print(f"数据库：{settings.resolved_database_url}")
            if not info.get("enabled"):
                print(f"未执行：{info['reason']}")
                return 0
            print(f"保留天数：{settings.storage.retention_days}（cutoff={info['cutoff']}）")
            print(
                "将清理：过期任务产物 {tasks} 个任务的 {files} 个文件（{size}）、"
                "孤儿目录 {orphan_dirs} 个、过期上传 {uploads} 个文件（{up}）".format(
                    size=_mb(info["bytes"]), up=_mb(info["upload_bytes"]), **info
                )
            )
            print("（干跑结束，未改动任何文件。）")
            return 0

        result = purge_artifacts(settings, session)
        if not result.get("enabled"):
            print(f"未执行：{result['reason']}")
            return 0
        print(f"保留天数：{settings.storage.retention_days}（cutoff={result['cutoff']}）")
        print(
            "已清理：任务产物 {tasks} 个、孤儿目录 {orphan_dirs} 个、上传 {uploads} 个；"
            "共 {files} 个文件、{size}".format(size=_mb(result["bytes"]), **result)
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
