# -*- coding: utf-8 -*-
"""AI 助手接口行为：鉴权、未配置降级、SSE 协议、上下文注入。

provider 一律 mock（不花钱、不依赖外网）：测的是**协议与编排**，
不是某家模型服务今天是否正常。
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import AIConfig, load_settings
from app.main import create_app
from app.routers import ai as ai_router
from app.services.ai.provider import AiUnavailableError


@pytest.fixture()
def settings_ai_configured(tmp_path):
    """启用且 key 齐全的配置（base_url 指向不存在的地址也无妨：provider 被 mock）。"""
    base = load_settings()
    return replace(
        base,
        data_dir=tmp_path / "data",
        server=replace(base.server, serve_frontend=False),
        database=replace(base.database, url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}"),
        storage=replace(
            base.storage,
            outputs_dir=str(tmp_path / "outputs"),
            uploads_dir=str(tmp_path / "uploads"),
        ),
        inference=replace(base.inference, preload=False),
        # BYOK 之后全局不再有凭证：chat 能不能跑取决于账号是否在网页里配置
        ai=AIConfig(
            enabled=True,
            # 测试不碰外网：向量路关掉（provider 已被 mock，这里再关 embedding）
            vector_search=False,
        ),
    )


@pytest.fixture()
def client_configured(settings_ai_configured):
    app = create_app(settings_ai_configured)
    with TestClient(app) as c:
        yield c, settings_ai_configured


def _auth(client: TestClient) -> dict[str, str]:
    creds = {"username": "aiuser", "password": "aiuser123456"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _auth_with_ai(client: TestClient, username: str = "aiuser") -> dict[str, str]:
    """注册 + 登录 + 在网页端保存 BYOK 配置（聊天接口的前置条件）。"""
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


# ---------------------------------------------------------------- 鉴权与状态
def test_status_requires_auth(client):
    assert client.get("/api/ai/status").status_code == 401


def test_status_reports_per_account_configuration(client):
    """BYOK：status 的 configured 只看当前账号有没有自己的配置。"""
    headers = _auth(client)
    body = client.get("/api/ai/status", headers=headers).json()
    assert body["enabled"] is True
    assert body["configured"] is False
    assert body["model"]  # 未配置时回显全局默认模型名

    client.put(
        "/api/ai/account",
        headers=headers,
        json={"base_url": "http://mock.local/v1", "api_key": "k", "model": "m1"},
    )
    body = client.get("/api/ai/status", headers=headers).json()
    assert body["configured"] is True
    assert body["model"] == "m1"


# ---------------------------------------------------------------- 未配置降级
def test_chat_503_when_not_configured(client):
    headers = _auth(client)
    resp = client.post("/api/ai/chat", headers=headers, json={"question": "虫情如何"})
    assert resp.status_code == 503
    assert "配置" in resp.json()["detail"]


def test_chat_503_when_disabled(client, monkeypatch):
    headers = _auth(client)
    # 直接把 app.state.settings.ai.enabled 关掉（不改真实文件）
    # frozen dataclass 不能就地改字段：整体换成 ai.enabled=False 的新 settings
    # （get_settings 每次请求都从 app.state 读，换对象即刻生效）
    client.app.state.settings = replace(
        client.app.state.settings,
        ai=replace(client.app.state.settings.ai, enabled=False),
    )
    resp = client.post("/api/ai/chat", headers=headers, json={"question": "虫情如何"})
    assert resp.status_code == 503


# ---------------------------------------------------------------- SSE 协议
def _fake_stream(chunks):
    def fake(config, messages, usage_out=None):
        yield from chunks

    return fake


def test_chat_streams_deltas_and_done(client_configured, monkeypatch):
    client, _ = client_configured
    headers = _auth_with_ai(client)
    monkeypatch.setattr(
        ai_router, "stream_chat", _fake_stream(["近 7 天", "检出 12 只", "蚜虫。"])
    )
    with client.stream("POST", "/api/ai/chat", headers=headers, json={"question": "棉铃虫怎么防治"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse_events(resp)

    assert [e["type"] for e in events] == ["meta", "delta", "delta", "delta", "done"]
    deltas = [e for e in events if e["type"] == "delta"]
    assert "".join(e["text"] for e in deltas) == "近 7 天检出 12 只蚜虫。"
    done = events[-1]
    assert done["citations"], "知识类问题应带回引用"
    assert done["citations"][0]["zh_name"] == "棉铃虫"
    assert done["citations"][0]["index"] == 1
    assert done["elapsed_ms"] >= 0


def test_chat_error_event_mid_stream(client_configured, monkeypatch):
    client, _ = client_configured
    headers = _auth_with_ai(client)

    def failing(config, messages, usage_out=None):
        yield "半段回答"
        raise AiUnavailableError("模型服务暂时不可用")

    monkeypatch.setattr(ai_router, "stream_chat", failing)
    with client.stream("POST", "/api/ai/chat", headers=headers, json={"question": "你好"}) as resp:
        events = _parse_sse_events(resp)

    # 协议约定：已吐出的内容保留，末尾补 error 事件（HTTP 仍是 200）
    assert [e["type"] for e in events] == ["meta", "delta", "error"]
    assert "模型服务暂时不可用" in events[-1]["message"]


def test_chat_injects_data_context(client_configured, monkeypatch):
    """编排层必须把实时数据写进 system —— 这是"不许编数字"的物质基础。"""
    client, _ = client_configured
    headers = _auth_with_ai(client)
    captured: dict = {}

    def fake(config, messages, usage_out=None):
        captured["messages"] = messages
        yield "好"

    monkeypatch.setattr(ai_router, "stream_chat", fake)
    client.post("/api/ai/chat", headers=headers, json={"question": "这周虫情怎么样"})
    system = captured["messages"][0]["content"]
    assert "实时虫情数据" in system
    assert "没有检测记录" in system  # 空库也必须显式说明，而不是消失
    assert "引用编号" in system


def test_chat_history_is_trimmed_and_passed(client_configured, monkeypatch):
    client, _ = client_configured
    headers = _auth_with_ai(client)
    captured: dict = {}

    def fake(config, messages, usage_out=None):
        captured["messages"] = messages
        yield "好"

    monkeypatch.setattr(ai_router, "stream_chat", fake)
    history = [
        {"role": "user", "content": f"第{i}轮问题"} if i % 2 == 0
        else {"role": "assistant", "content": f"第{i}轮回答"}
        for i in range(10)
    ]
    client.post(
        "/api/ai/chat",
        headers=headers,
        json={"question": "最新的问题", "history": history},
    )
    roles = [m["role"] for m in captured["messages"]]
    assert roles[0] == "system" and roles[-1] == "user"
    # 10 条历史被 history_limit=8 截到最近 8 条
    assert len(captured["messages"]) == 1 + 8 + 1


# ---------------------------------------------------------------- 输入校验
def test_chat_validates_question_and_history(client_configured):
    client, _ = client_configured
    headers = _auth(client)
    assert client.post("/api/ai/chat", headers=headers, json={"question": ""}).status_code == 422
    assert (
        client.post("/api/ai/chat", headers=headers, json={"question": "x" * 501}).status_code
        == 422
    )
    too_many = {"question": "hi", "history": [{"role": "user", "content": "x"}] * 13}
    assert client.post("/api/ai/chat", headers=headers, json=too_many).status_code == 422


# ---------------------------------------------------------------- 工具
def _parse_sse_events(resp) -> list[dict]:
    import json

    events = []
    for line in resp.iter_lines():
        line = line.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):]))
    return events
