# -*- coding: utf-8 -*-
"""告警中心的接口测试。

此前 `/api/alerts` 四个接口**没有任何测试覆盖**（只在别的流程里被间接用到），
而告警是本系统里唯一"会主动要求人处理"的东西，值得单独钉住：

  * 四个接口都必须登录（它们是管理侧能力，不能匿名）
  * 列表的三种筛选（acked / level / limit）
  * 单条确认与全部确认，以及确认不存在的 id 要 404
  * 规则回显的结构（前端要靠它解释"为什么会报警"）
  * **规则真的会触发并落库** —— 直接构造一条超阈值任务跑 evaluate_task
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Alert, Task, TaskMode, TaskStatus, utcnow
from app.services.alerts import evaluate_task, load_rules


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    creds = {"username": "alertuser", "password": "alertuser12345"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _insert_alert(app, *, message: str = "测试告警", level: str = "warn", acked: bool = False) -> int:
    """直接写库造一条告警：本文件的重点是接口与规则，不必绕检测流程。"""
    session = app.state.session_factory()
    try:
        row = Alert(message=message, level=level, acked=acked, ts=utcnow())
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)
    finally:
        session.close()


# ---------------------------------------------------------------- 鉴权
def test_all_alert_endpoints_require_login(client: TestClient):
    assert client.get("/api/alerts").status_code == 401
    assert client.get("/api/alerts/rules").status_code == 401
    assert client.post("/api/alerts/1/ack").status_code == 401
    assert client.post("/api/alerts/ack-all").status_code == 401


# ---------------------------------------------------------------- 列表与筛选
def test_list_returns_all_by_default_and_filters_work(app, client: TestClient, auth_headers):
    _insert_alert(app, message="未确认-警告", level="warn", acked=False)
    _insert_alert(app, message="未确认-严重", level="critical", acked=False)
    _insert_alert(app, message="已确认", level="info", acked=True)

    all_rows = client.get("/api/alerts", headers=auth_headers).json()
    assert len(all_rows) == 3

    unacked = client.get("/api/alerts", headers=auth_headers, params={"acked": "false"}).json()
    assert len(unacked) == 2
    assert all(row["acked"] is False for row in unacked)

    critical = client.get("/api/alerts", headers=auth_headers, params={"level": "critical"}).json()
    assert len(critical) == 1
    assert critical[0]["message"] == "未确认-严重"

    limited = client.get("/api/alerts", headers=auth_headers, params={"limit": 1}).json()
    assert len(limited) == 1


# ---------------------------------------------------------------- 确认
def test_ack_one(app, client: TestClient, auth_headers):
    alert_id = _insert_alert(app, message="待确认")
    resp = client.post(f"/api/alerts/{alert_id}/ack", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["acked"] is True

    remaining = client.get("/api/alerts", headers=auth_headers, params={"acked": "false"}).json()
    assert all(row["id"] != alert_id for row in remaining)


def test_ack_unknown_id_returns_404(client: TestClient, auth_headers):
    assert client.post("/api/alerts/999999/ack", headers=auth_headers).status_code == 404


def test_ack_all(app, client: TestClient, auth_headers):
    _insert_alert(app, acked=False)
    _insert_alert(app, acked=False)
    result = client.post("/api/alerts/ack-all", headers=auth_headers).json()
    assert result["acked"] == 2

    unacked = client.get("/api/alerts", headers=auth_headers, params={"acked": "false"}).json()
    assert unacked == []


# ---------------------------------------------------------------- 规则
def test_rules_endpoint_exposes_shipped_rules(client: TestClient, auth_headers):
    payload = client.get("/api/alerts/rules", headers=auth_headers).json()
    assert "rules" in payload and "webhook" in payload
    rules = payload["rules"]
    assert rules, "随项目发布的 alert_rules.yaml 不应为空"
    first = rules[0]
    # 前端要靠这几个字段解释"为什么报警"、并显示阈值
    assert "name" in first
    assert "threshold" in first
    assert first.get("level") in ("info", "warn", "critical")


# ---------------------------------------------------------------- 规则真的会触发
def test_rule_hit_creates_alert(app):
    """构造一条超阈值任务，确认 evaluate_task 真的写入了告警。

    阈值取规则文件里的最大值 + 1，这样无论规则怎么调，这条用例都成立
    （而不是写死 100 这种和配置耦合的数字）。
    """
    rules = load_rules().get("rules", [])
    thresholds = [
        float(rule["threshold"])
        for rule in rules
        if isinstance(rule, dict) and rule.get("threshold") is not None
    ]
    assert thresholds, "规则文件里应有带 threshold 的规则"
    triggered_value = int(max(thresholds)) + 1

    session = app.state.session_factory()
    try:
        task = Task(
            mode=TaskMode.IMAGE,
            status=TaskStatus.DONE,
            source_name="阈值触发用例",
            started_at=utcnow(),
            unique_objects=triggered_value,
            total_objects=triggered_value,
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        created = evaluate_task(session, task)
        assert created, "超过所有阈值却没有产生告警"
        assert all(alert.task_id == task.id for alert in created)
    finally:
        session.close()
