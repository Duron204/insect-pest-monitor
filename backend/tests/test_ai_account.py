# -*- coding: utf-8 -*-
"""账号级 AI 配置（BYOK）：CRUD、脱码、密钥留空保持、账号隔离。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.routers import ai as ai_router
from test_ai_router import client_configured, settings_ai_configured  # noqa: F401


def _register(client: TestClient, username: str) -> dict[str, str]:
    creds = {"username": username, "password": f"{username}123456"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_requires_auth(client):
    assert client.get("/api/ai/account").status_code == 401
    assert client.put("/api/ai/account", json={}).status_code == 401


def test_config_crud_and_masking(client_configured):
    client, _ = client_configured
    headers = _register(client, "byok1")

    # 未配置：404
    assert client.get("/api/ai/account", headers=headers).status_code == 404

    # 首次配置必须带密钥
    bad = client.put(
        "/api/ai/account", headers=headers,
        json={"base_url": "http://mock.local/v1", "api_key": "", "model": "m"},
    )
    assert bad.status_code == 400

    # 保存成功：回显脱码，绝不含完整密钥
    saved = client.put(
        "/api/ai/account", headers=headers,
        json={"base_url": "http://mock.local/v1", "api_key": "abcdefgh12345678", "model": "m1"},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["has_key"] is True
    assert body["api_key_preview"] == "ab****5678"
    assert "abcdefgh12345678" not in body["api_key_preview"]
    raw = client.get("/api/ai/account", headers=headers).json()
    assert raw["base_url"] == "http://mock.local/v1"

    # 密钥留空 = 保持原值（只改 base_url）
    updated = client.put(
        "/api/ai/account", headers=headers,
        json={"base_url": "http://other.local/v1", "api_key": "", "model": "m2"},
    )
    assert updated.status_code == 200
    assert updated.json()["has_key"] is True  # 密钥还在
    assert updated.json()["base_url"] == "http://other.local/v1"

    # 清除 → 404 → 状态回到未配置
    assert client.delete("/api/ai/account", headers=headers).status_code == 204
    assert client.get("/api/ai/account", headers=headers).status_code == 404
    assert client.get("/api/ai/status", headers=headers).json()["configured"] is False


def test_config_is_between_accounts(client_configured):
    """配置不互通：B 读不到 A 的配置，B 未配置时聊天 503。"""
    client, _ = client_configured
    headers_a = _register(client, "byoka")
    headers_b = _register(client, "byokb")
    client.put(
        "/api/ai/account", headers=headers_a,
        json={"base_url": "http://mock.local/v1", "api_key": "key-of-a", "model": "m"},
    )

    assert client.get("/api/ai/account", headers=headers_b).status_code == 404
    assert client.get("/api/ai/status", headers=headers_b).json()["configured"] is False

    resp = client.post("/api/ai/chat", headers=headers_b, json={"question": "你好"})
    assert resp.status_code == 503  # B 没有自己的配置


def test_test_endpoint_uses_saved_key_when_blank(client_configured, monkeypatch):
    """试连：密钥留空时用已保存值；成功与失败都返回 200 + ok 标志。"""
    client, _ = client_configured
    headers = _register(client, "byok2")
    client.put(
        "/api/ai/account", headers=headers,
        json={"base_url": "http://mock.local/v1", "api_key": "saved-key", "model": "m"},
    )

    captured: dict = {}

    def fake(config, messages, usage_out=None):
        captured["config"] = config
        yield "ok"

    monkeypatch.setattr(ai_router, "stream_chat", fake)
    result = client.post(
        "/api/ai/account/test", headers=headers,
        json={"base_url": "http://mock.local/v1", "api_key": "", "model": "m"},
    ).json()
    assert result["ok"] is True
    assert captured["config"].api_key == "saved-key"  # 用了保存的密钥
    assert captured["config"].max_tokens == 8  # 试连是轻量请求

    def failing(config, messages, usage_out=None):
        raise ai_router.AiUnavailableError("连不上")
        yield  # pragma: no cover

    monkeypatch.setattr(ai_router, "stream_chat", failing)
    result = client.post(
        "/api/ai/account/test", headers=headers,
        json={"base_url": "http://mock.local/v1", "api_key": "", "model": "m"},
    ).json()
    assert result["ok"] is False and "连不上" in result["message"]
