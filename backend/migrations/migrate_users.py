# -*- coding: utf-8 -*-
"""把旧桌面端用户库 `UserDatabase.db` 迁移到新后端的 `app.db`。

旧库结构（本机实测）：
    CREATE TABLE users (username TEXT PRIMARY KEY, password TEXT, avatar TEXT)
    password 列是【无盐 SHA-256 的 hex64】（没有 salt 列）

**为什么不能直接转成 bcrypt**：哈希是单向的，拿不到明文就无法重新哈希。
因此采用「旧算法校验 + 登录时透明升级」策略：
  1. 旧的 hex64 原样写入 `users.legacy_sha256`，`password_hash` 留空；
  2. 用户第一次用旧口令登录时，`routers/auth.py` 用旧算法校验通过后，
     立即写入 bcrypt 哈希并清空 `legacy_sha256`。

安全提示：无盐 SHA-256 可被彩虹表快速破解，建议迁移后提醒用户改口令。

用法：
    D:\\python\\envs\\yolov5\\python.exe backend\\migrations\\migrate_users.py --dry-run
    D:\\python\\envs\\yolov5\\python.exe backend\\migrations\\migrate_users.py
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import load_settings  # noqa: E402
from app.db import create_all, init_engine, new_session  # noqa: E402
from app.models import User  # noqa: E402

DEFAULT_LEGACY_DB = PROJECT_ROOT / "UserDatabase.db"
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_FALLBACK_AVATAR = PROJECT_ROOT / "icons" / "user.png"


def read_legacy(path: Path) -> list[dict]:
    """读取旧库。只读打开，绝不修改原文件。"""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT username, password, avatar FROM users").fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def looks_like_legacy_hash(value: str | None) -> bool:
    return bool(value) and bool(_SHA256_HEX.match(value.strip()))


def _resolve_avatar(raw: str | None) -> Path | None:
    """旧库的 avatar 多为失效的绝对路径（如 H:/PySideDesign/...），尽力解析。"""
    if not raw:
        return None
    candidate = Path(raw)
    for path in (candidate, PROJECT_ROOT / raw.lstrip("./\\"), PROJECT_ROOT / "icons" / candidate.name):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return _FALLBACK_AVATAR if _FALLBACK_AVATAR.is_file() else None


def copy_avatar(raw: str | None, username: str, settings) -> str | None:
    """把旧头像复制进 data/avatars 并返回前端可用的 URL；找不到则返回 None。"""
    source = _resolve_avatar(raw)
    if source is None:
        return None
    target_dir = settings.data_dir / "avatars"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^0-9A-Za-z._\u4e00-\u9fff-]", "_", username)[:40] or "user"
    target = target_dir / f"legacy_{safe_name}{source.suffix.lower() or '.png'}"
    try:
        shutil.copyfile(str(source), str(target))
    except OSError:
        return None
    return f"/media/avatars/{target.name}"


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移旧用户库到后端 app.db")
    parser.add_argument(
        "--legacy-db", default=str(DEFAULT_LEGACY_DB), help="旧 UserDatabase.db 路径"
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    args = parser.parse_args()

    settings = load_settings()
    settings.ensure_dirs()
    init_engine(settings.resolved_database_url)
    create_all()

    legacy_path = Path(args.legacy_db)
    if not legacy_path.exists():
        print(f"[错误] 找不到旧用户库：{legacy_path}")
        return 1

    print(f"旧库：{legacy_path}")
    print(f"新库：{settings.sqlite_path}")
    rows = read_legacy(legacy_path)
    print(f"旧库用户数：{len(rows)}")
    print("-" * 56)

    db = new_session()
    created = skipped = needs_reset = 0
    try:
        for row in rows:
            username = (row.get("username") or "").strip()
            if not username:
                skipped += 1
                continue

            existing = db.query(User).filter(User.username == username).one_or_none()
            if existing is not None:
                print(f"  跳过（新库已存在）：{username}")
                skipped += 1
                continue

            raw_password = row.get("password")
            is_legacy = looks_like_legacy_hash(raw_password)
            user = User(
                username=username,
                password_hash=None,
                legacy_sha256=(raw_password.strip() if is_legacy else None),
                # dry-run 不应产生任何副作用，包括复制头像文件
                avatar_path=(None if args.dry_run else copy_avatar(row.get("avatar"), username, settings)),
                # 口令格式不是预期的 hex64 -> 无法用旧算法校验，只能强制重设
                must_reset_password=not is_legacy,
            )
            db.add(user)
            created += 1
            if not is_legacy:
                needs_reset += 1
            note = "可沿用旧口令（首次登录自动升级为 bcrypt）" if is_legacy else "口令格式未知，需重设"
            print(f"  迁移：{username}  ->  {note}")

        if args.dry_run:
            db.rollback()
            print("-" * 56)
            print(f"dry-run：将迁移 {created} 个用户，跳过 {skipped} 个。未写入数据库。")
        else:
            db.commit()
            print("-" * 56)
            print(f"完成：迁移 {created} 个，跳过 {skipped} 个。")
            if needs_reset:
                print(f"注意：其中 {needs_reset} 个用户口令格式无法识别，首次登录后需重设口令。")
            print("提示：旧库为无盐 SHA-256，安全性弱，建议提醒这些用户尽快修改口令。")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
