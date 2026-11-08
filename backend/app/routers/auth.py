# -*- coding: utf-8 -*-
"""鉴权路由：注册 / 登录 / 当前用户 / 改口令 / 头像。

登录流程里包含**旧库透明升级**：
`UserDatabase.db` 存的是无盐 SHA-256，无法预先转成 bcrypt（哈希单向），
因此首次用旧口令登录成功时，才顺手写入 bcrypt 并清空 legacy_sha256。
"""
from __future__ import annotations

import hashlib
import io
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_current_user, get_db, get_settings
from ..models import User, utcnow
from ..schemas import (
    AvatarOut,
    MessageOut,
    PasswordChange,
    TokenOut,
    UserCreate,
    UserLogin,
    UserOut,
)
from ..security import (
    create_access_token,
    hash_password,
    legacy_sha256,
    verify_password,
)
from ..services.storage import UploadTooLarge, read_upload_limited

router = APIRouter(prefix="/auth", tags=["auth"])

_ALLOWED_AVATAR_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_AVATAR_MB = 2
_MAX_AVATAR_BYTES = _MAX_AVATAR_MB * 1024 * 1024   # 2 MB

# 统一的口令错误文案：不区分「用户不存在」与「密码错误」，避免用户名枚举
_BAD_CREDENTIALS = "用户名或密码不正确"


def _to_token_out(user: User, settings: Settings) -> TokenOut:
    token, expires_in = create_access_token(
        subject=str(user.id),
        secret=settings.resolved_jwt_secret,
        expires_hours=settings.auth.jwt_expire_hours,
        algorithm=settings.auth.algorithm,
        extra={"username": user.username},
    )
    return TokenOut(
        access_token=token,
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    payload: UserCreate,
    db: Session = Depends(get_db),
) -> UserOut:
    exists = db.scalar(select(User).where(User.username == payload.username))
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该用户名已被注册")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        avatar_path=payload.avatar_path,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenOut)
def login(
    payload: UserLogin,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenOut:
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_CREDENTIALS)

    authenticated = verify_password(payload.password, user.password_hash)

    # 旧库兼容：bcrypt 未命中时，回退到无盐 SHA-256 校验
    if not authenticated and user.legacy_sha256:
        candidate = legacy_sha256(payload.password)
        if secrets.compare_digest(candidate, user.legacy_sha256):
            authenticated = True
            # 透明升级：立即换成 bcrypt，并清掉旧哈希
            user.password_hash = hash_password(payload.password)
            user.legacy_sha256 = None

    if not authenticated:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_CREDENTIALS)

    user.last_login_at = utcnow()
    db.commit()
    db.refresh(user)
    return _to_token_out(user, settings)


@router.get("/me", response_model=UserOut)
def me(current: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(current)


@router.post("/password", response_model=MessageOut)
def change_password(
    payload: PasswordChange,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    """改口令，也用于旧库迁移用户的强制重设。"""
    ok = verify_password(payload.old_password, current.password_hash)
    if not ok and current.legacy_sha256:
        if secrets.compare_digest(legacy_sha256(payload.old_password), current.legacy_sha256):
            ok = True
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码不正确")

    current.password_hash = hash_password(payload.new_password)
    current.legacy_sha256 = None
    current.must_reset_password = False
    db.commit()
    return MessageOut(message="密码已更新")


@router.post("/avatar", response_model=AvatarOut)
async def upload_avatar(
    file: UploadFile = File(...),
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AvatarOut:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_AVATAR_EXT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"仅支持图片格式：{', '.join(sorted(_ALLOWED_AVATAR_EXT))}",
        )

    # 边读边数，超限立刻断 —— 不能先 `await file.read()` 再比大小：
    # 那样限制的只是"允不允许存"，而来人已经能让服务端分配一份任意大的内存了。
    try:
        data = await read_upload_limited(file, _MAX_AVATAR_MB)
    except UploadTooLarge:
        raise HTTPException(
            status_code=413,  # 同上：避免绑定会随 Starlette 版本改名的常量
            detail=f"头像不能超过 {_MAX_AVATAR_MB} MB",
        ) from None
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件为空")

    # 真正验证是不是图片，而不是只信扩展名
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不是有效的图片文件")

    avatar_dir = settings.data_dir / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:12]
    filename = f"u{current.id}_{digest}{suffix}"
    (avatar_dir / filename).write_bytes(data)

    current.avatar_path = f"/media/avatars/{filename}"
    db.commit()
    return AvatarOut(avatar_path=current.avatar_path)


@router.post("/logout", response_model=MessageOut)
def logout(current: User = Depends(get_current_user)) -> MessageOut:
    """JWT 无状态，服务端无可撤销状态；前端清除本地 token 即可。

    保留该端点是为了前端有统一的登出调用点，将来若引入令牌黑名单可直接扩展。
    """
    return MessageOut(message="已登出")
