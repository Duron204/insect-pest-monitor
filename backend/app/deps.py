# -*- coding: utf-8 -*-
"""FastAPI 依赖注入。

统一从 `request.app.state` 取配置与数据库会话工厂，
这样测试里换一个 app 实例就能完全隔离，不依赖模块级全局变量。
"""
from __future__ import annotations

from collections.abc import Iterator

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Device, DeviceStatus, User
from .security import decode_access_token, hash_device_token, verify_device_token

# auto_error=False：由我们自己抛 401，保证错误体格式统一
bearer_scheme = HTTPBearer(auto_error=False)

# 设备令牌走**独立的头**，不复用 Authorization。理由：复用会让"用户 JWT"与
# "设备令牌"挤在同一个头里，任何一处 scheme 解析写错就会把设备当成用户（或反过来），
# 而这两种身份的权限完全不同 —— 宁可多一个头，也不要这种含糊。
device_token_header = APIKeyHeader(name="X-Device-Token", auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    db: Session = factory()
    try:
        yield db
    finally:
        db.close()


def _unauthorized(detail: str = "未认证或登录已过期") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _user_from_token(token: str, db: Session, settings: Settings) -> User:
    try:
        # **必须**是 decode_access_token，不是 decode_token：后者会放行带 `purpose`
        # 的令牌，而 MJPEG 票据就是那种 —— 它按设计要进 query string，注定会泄漏，
        # 一旦被当成登录态就是一张全权账号凭证。见 security.py 的注释。
        payload = decode_access_token(
            token, settings.resolved_jwt_secret, settings.auth.algorithm
        )
    except pyjwt.ExpiredSignatureError:
        raise _unauthorized("登录已过期，请重新登录")
    except pyjwt.PyJWTError:
        raise _unauthorized("令牌无效")

    subject = payload.get("sub")
    if not subject:
        raise _unauthorized("令牌缺少主体信息")

    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        raise _unauthorized("令牌主体格式错误")

    user = db.get(User, user_id)
    if user is None:
        raise _unauthorized("用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用")
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    if credentials is None or not credentials.credentials:
        raise _unauthorized("缺少访问令牌")
    return _user_from_token(credentials.credentials, db, settings)


def get_current_device(
    token: str | None = Depends(device_token_header),
    db: Session = Depends(get_db),
) -> Device:
    """设备令牌鉴权。面向盒子的接口用它，与 `get_current_user` 完全并列。

    设备身份**只从令牌来**，绝不从路径或请求体来 —— 否则一台设备就能冒充另一台。
    """
    if not token:
        raise _unauthorized("缺少设备令牌")
    # 按唯一索引查；再常量时间复核一次，两道都对才认
    device = db.scalars(select(Device).where(Device.token_hash == hash_device_token(token))).first()
    if device is None or not verify_device_token(token, device.token_hash):
        raise _unauthorized("设备令牌无效")
    if device.status != DeviceStatus.ACTIVE:
        # 403 而不是 401：令牌本身没问题，是它背后的绑定关系没了。
        # 盒子收到这条会清掉本地令牌重新配对，而不是无谓地重试。
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="设备已解绑，请重新配对"
        )
    return device


def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User | None:
    """允许匿名访问的接口使用（例如只读的物种知识库）。"""
    if credentials is None or not credentials.credentials:
        return None
    return _user_from_token(credentials.credentials, db, settings)

