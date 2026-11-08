# -*- coding: utf-8 -*-
"""在线备份 SQLite 数据库，并轮转旧备份。

现场只有一个 `backend/data/app.db`（账号、历史、告警、模型注册表都在里面），
SD 卡坏掉或误删就全丢 —— 这个脚本就是那份兜底。

用 SQLite 的**在线备份 API** 而不是复制文件：服务正在写入时直接 copy 可能拷到
不一致的中间状态；备份 API 是原子的，而且**不需要停服务**。

用法（项目根目录、用后端的解释器）：

    python tools/backup_db.py                 # 备份到 backend/data/backups/
    python tools/backup_db.py --keep 30       # 保留最近 30 份（默认取配置）
    python tools/backup_db.py --no-verify     # 跳过备份后的完整性校验

现场用 `deploy/insect-maintenance.timer` 每天跑一次。
**注意**：备份与数据库在同一个 SD 卡上，防的是"误删/写坏"而不是"卡坏"；
防范卡坏请把 backups/ 定期同步到 U 盘或另一台机器。
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
from app.services.maintenance import backup_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="在线备份 SQLite 数据库")
    parser.add_argument("--keep", type=int, help="保留份数（0 表示不轮转）")
    parser.add_argument("--no-verify", action="store_true", help="跳过完整性校验")
    args = parser.parse_args()

    settings = load_settings()
    if args.keep is not None:
        settings = replace(settings, storage=replace(settings.storage, backup_keep=args.keep))

    try:
        result = backup_database(settings, keep=args.keep, verify=not args.no_verify)
    except FileNotFoundError as exc:
        print(f"备份失败：{exc}")
        return 1

    print(f"备份完成：{result['path']}（{result['bytes'] / 1e6:.2f} MB）")
    if result["integrity"] is not None:
        print(f"  完整性校验：{result['integrity']}")
        counts = "、".join(f"{k}={v}" for k, v in result["counts"].items())
        print(f"  各表行数：{counts}")
    if result["removed"]:
        print(f"  轮转删除：{'、'.join(result['removed'])}")
    print(f"  保留份数：{result['keep']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
