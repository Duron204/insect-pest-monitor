# -*- coding: utf-8 -*-
"""AI 会话持久化 / 用量观测 / 诊断报告 的行为测试。

provider 一律 mock：测的是**记账与归属**，不是模型服务。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.routers import ai as ai_router
from test_ai_router import client_configured, settings_ai_configured  # noqa: F401  复用已配置 app 的夹具


def _fake_stream(texts, usage=None):
    def fake(config, messages, usage_out=None):
        for t in texts:
            yield t
        if usage_out is not None and usage:
            usage_out.update(usage)

    return fake


def _auth(client: TestClient, username: str) -> dict[str, str]:
    """注册 + 登录 + 网页端保存 BYOK 配置（这些用例都要真正发起对话）。"""
    creds = {"username": username, "password": f"{username}123456"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.put(
        "/api/ai/account",
        headers=headers,
        json={"base_url": "http://mock.local/v1", "api_key": "test-key", "model": "mock-model"},
    )
    assert resp.status_code == 200
    return headers


def _parse(resp) -> list[dict]:
    import json

    events = []
    for line in resp.iter_lines():
        line = line.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):]))
    return events


def _events(client, headers, body):
    with client.stream("POST", "/api/ai/chat", headers=headers, json=body) as resp:
        assert resp.status_code == 200
        return _parse(resp)


@pytest.fixture()
def configured(client_configured):
    return client_configured


# ---------------------------------------------------------------- 对话持久化
def test_chat_creates_conversation_and_saves_messages(configured, monkeypatch):
    client, _ = configured
    headers = _auth(client, "hist1")
    monkeypatch.setattr(
        ai_router, "stream_chat", _fake_stream(["菜螟", "防治", "建议。"], {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
    )
    events = _events(client, headers, {"question": "白菜心叶被虫蛀了怎么办"})

    meta = events[0]
    assert meta["type"] == "meta" and meta["conversation_id"] > 0
    done = events[-1]
    assert done["type"] == "done"
    assert done["usage"]["total_tokens"] == 150
    assert done["conversation_id"] == meta["conversation_id"]

    conversations = client.get("/api/ai/conversations", headers=headers).json()
    assert len(conversations) == 1
    assert conversations[0]["message_count"] == 2
    assert "白菜" in conversations[0]["title"]

    messages = client.get(
        f"/api/ai/conversations/{meta['conversation_id']}", headers=headers
    ).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "菜螟防治建议" in messages[1]["content"]
    assert messages[1]["total_tokens"] == 150
    assert messages[1]["citations"], "知识类问题应保存引用"


def test_chat_followup_appends(configured, monkeypatch):
    client, _ = configured
    headers = _auth(client, "hist2")
    monkeypatch.setattr(ai_router, "stream_chat", _fake_stream(["好"]))
    events = _events(client, headers, {"question": "第一问"})
    conv_id = events[0]["conversation_id"]
    _events(client, headers, {"question": "追问", "conversation_id": conv_id})

    messages = client.get(f"/api/ai/conversations/{conv_id}", headers=headers).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]


def test_user_message_survives_model_failure(configured, monkeypatch):
    """模型挂了：用户那一问仍然要留档（聊天记录是用户数据，不是缓存）。"""
    client, _ = configured
    headers = _auth(client, "hist3")

    def failing(config, messages, usage_out=None):
        raise ai_router.AiUnavailableError("down")

    monkeypatch.setattr(ai_router, "stream_chat", failing)
    events = _events(client, headers, {"question": "会失败的问题"})
    assert events[-1]["type"] == "error"

    conv_id = events[0]["conversation_id"]
    messages = client.get(f"/api/ai/conversations/{conv_id}", headers=headers).json()
    assert [m["role"] for m in messages] == ["user"]


def test_conversation_isolation(configured, monkeypatch):
    client, _ = configured
    headers_a = _auth(client, "owner1")
    headers_b = _auth(client, "owner2")
    monkeypatch.setattr(ai_router, "stream_chat", _fake_stream(["好"]))
    events = _events(client, headers_a, {"question": "我的问题"})
    conv_id = events[0]["conversation_id"]

    # B 看不到 A 的会话：列表为空、详情 404、删除 404
    assert client.get("/api/ai/conversations", headers=headers_b).json() == []
    assert client.get(f"/api/ai/conversations/{conv_id}", headers=headers_b).status_code == 404
    assert (
        client.delete(f"/api/ai/conversations/{conv_id}", headers=headers_b).status_code == 404
    )


def test_delete_conversation(configured, monkeypatch):
    client, _ = configured
    headers = _auth(client, "hist4")
    monkeypatch.setattr(ai_router, "stream_chat", _fake_stream(["好"]))
    events = _events(client, headers, {"question": "待删除"})
    conv_id = events[0]["conversation_id"]

    assert client.delete(f"/api/ai/conversations/{conv_id}", headers=headers).status_code == 204
    assert client.get(f"/api/ai/conversations/{conv_id}", headers=headers).status_code == 404
    assert client.get("/api/ai/conversations", headers=headers).json() == []


# ---------------------------------------------------------------- 用量观测
def test_usage_summary(configured, monkeypatch):
    client, _ = configured
    headers = _auth(client, "usage1")
    monkeypatch.setattr(
        ai_router,
        "stream_chat",
        _fake_stream(["答"], {"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100}),
    )
    _events(client, headers, {"question": "问题一"})
    _events(client, headers, {"question": "问题二"})

    summary = client.get("/api/ai/usage/summary", headers=headers).json()
    assert summary["calls"] == 2
    assert summary["total_tokens"] == 200
    assert summary["prompt_tokens"] == 120
    assert summary["completion_tokens"] == 80
    assert summary["conversations"] == 2
    assert summary["by_day"] and summary["by_day"][0]["calls"] == 2


def test_usage_summary_isolated(configured, monkeypatch):
    client, _ = configured
    headers_a = _auth(client, "usagea")
    headers_b = _auth(client, "usageb")
    monkeypatch.setattr(
        ai_router, "stream_chat", _fake_stream(["答"], {"total_tokens": 100, "prompt_tokens": 50, "completion_tokens": 50})
    )
    _events(client, headers_a, {"question": "A 的问题"})
    summary_b = client.get("/api/ai/usage/summary", headers=headers_b).json()
    assert summary_b["calls"] == 0 and summary_b["total_tokens"] == 0


# ---------------------------------------------------------------- 诊断报告
def test_report_generate_list_detail_export(configured, monkeypatch):
    client, _ = configured
    headers = _auth(client, "rep1")
    monkeypatch.setattr(
        ai_router,
        "stream_chat",
        _fake_stream(
            ["## 一、虫情概述\n", "近 7 天检出 12 只。\n", "## 三、防治建议\n", "- 清园 [1]。"],
            {"prompt_tokens": 80, "completion_tokens": 70, "total_tokens": 150},
        ),
    )
    with client.stream("POST", "/api/ai/report?range_token=7d", headers=headers) as resp:
        assert resp.status_code == 200
        events = _parse(resp)
    done = events[-1]
    assert done["type"] == "done" and done["report_id"] > 0
    assert done["usage"]["total_tokens"] == 150

    reports = client.get("/api/ai/report", headers=headers).json()
    assert len(reports) == 1
    report_id = reports[0]["id"]
    assert reports[0]["total_tokens"] == 150

    detail = client.get(f"/api/ai/report/{report_id}", headers=headers).json()
    assert "虫情概述" in detail["content"]

    export = client.get(f"/api/ai/report/{report_id}/export", headers=headers)
    assert export.status_code == 200
    assert "wordprocessingml" in export.headers["content-type"]
    # docx 是 zip 容器：PK 魔数 + content 里出现标题文本片段
    assert export.content[:2] == b"PK"


def test_report_isolation(configured, monkeypatch):
    client, _ = configured
    headers_a = _auth(client, "repa")
    headers_b = _auth(client, "repb")
    monkeypatch.setattr(ai_router, "stream_chat", _fake_stream(["报告"]))
    with client.stream("POST", "/api/ai/report", headers=headers_a) as resp:
        events = _parse(resp)
    report_id = events[-1]["report_id"]
    assert client.get(f"/api/ai/report/{report_id}", headers=headers_b).status_code == 404
    assert client.get(f"/api/ai/report/{report_id}/export", headers=headers_b).status_code == 404


def test_report_requires_configuration(client):
    """未配置账号（_auth 会配置，这里手动注册一个干净账号）。"""
    creds = {"username": "rep3", "password": "rep3123456"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post("/api/ai/report", headers=headers)
    assert resp.status_code == 503
