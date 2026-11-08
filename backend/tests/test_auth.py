# -*- coding: utf-8 -*-
"""鉴权接口测试。

重点覆盖两处最容易出错的地方：
1. 口令哈希绝不外泄（响应体与数据库列都检查）；
2. 旧库无盐 SHA-256 的**透明升级**：首次用旧口令登录即换成 bcrypt。
"""
from __future__ import annotations

from app.models import User
from app.security import legacy_sha256


def _register(client, username: str = "alice", password: str = "secret123"):
    return client.post(
        "/api/auth/register", json={"username": username, "password": password}
    )


def _login(client, username: str = "alice", password: str = "secret123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 健康检查
def test_health_reports_unloaded_model(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["model_loaded"] is False
    assert body["device"] == "unloaded"


# ---------------------------------------------------------------- 注册
def test_register_success_hides_password(client):
    resp = _register(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == "alice"
    assert body["id"] > 0
    # 任何形式的哈希都不得出现在响应里
    assert "password" not in body
    assert "password_hash" not in body
    assert "legacy_sha256" not in body


def test_register_duplicate_username(client):
    assert _register(client).status_code == 201
    resp = _register(client)
    assert resp.status_code == 409


def test_register_rejects_short_password(client):
    resp = _register(client, password="123")
    assert resp.status_code == 422


# ---------------------------------------------------------------- 登录
def test_login_success_and_me(client):
    _register(client)
    resp = _login(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    token = body["access_token"]
    assert body["user"]["username"] == "alice"

    me = client.get("/api/auth/me", headers=_auth_header(token))
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


def test_login_wrong_password(client):
    _register(client)
    resp = _login(client, password="wrong-password")
    assert resp.status_code == 401


def test_login_unknown_user_same_message(client):
    """未知用户与错误口令必须返回相同文案，避免用户名枚举。"""
    _register(client)
    wrong_pw = _login(client, password="wrong-password")
    unknown = _login(client, username="nobody", password="secret123")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json()["detail"] == unknown.json()["detail"]


# ---------------------------------------------------------------- 令牌
def test_me_requires_token(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_rejects_garbage_token(client):
    resp = client.get("/api/auth/me", headers=_auth_header("not-a-jwt"))
    assert resp.status_code == 401


# ---------------------------------------------------------------- 改口令
def test_change_password_then_login_with_new(client):
    _register(client)
    token = _login(client).json()["access_token"]

    resp = client.post(
        "/api/auth/password",
        json={"old_password": "secret123", "new_password": "newsecret456"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 200, resp.text

    assert _login(client, password="secret123").status_code == 401
    assert _login(client, password="newsecret456").status_code == 200


def test_change_password_rejects_wrong_old(client):
    _register(client)
    token = _login(client).json()["access_token"]
    resp = client.post(
        "/api/auth/password",
        json={"old_password": "nope", "new_password": "newsecret456"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------- 旧库迁移
def test_legacy_sha256_transparent_upgrade(app, client):
    """模拟从 UserDatabase.db 迁移来的用户：只有无盐 SHA-256，没有 bcrypt。"""
    factory = app.state.session_factory
    db = factory()
    try:
        db.add(
            User(
                username="床前明月光",
                password_hash=None,
                legacy_sha256=legacy_sha256("oldpass123"),
            )
        )
        db.commit()
    finally:
        db.close()

    # 首次用旧口令登录：应当成功
    resp = _login(client, username="床前明月光", password="oldpass123")
    assert resp.status_code == 200, resp.text

    # 登录后应已静默升级为 bcrypt 且清空旧哈希
    db = factory()
    try:
        user = db.query(User).filter(User.username == "床前明月光").one()
        assert user.password_hash is not None
        assert user.password_hash.startswith("$2"), user.password_hash
        assert user.legacy_sha256 is None
    finally:
        db.close()

    # 升级后仍可用同一口令登录
    assert _login(client, username="床前明月光", password="oldpass123").status_code == 200


def test_legacy_hash_wrong_password_still_rejected(app, client):
    factory = app.state.session_factory
    db = factory()
    try:
        db.add(
            User(
                username="legacy2",
                password_hash=None,
                legacy_sha256=legacy_sha256("rightpass"),
            )
        )
        db.commit()
    finally:
        db.close()

    assert _login(client, username="legacy2", password="wrongpass").status_code == 401
