# -*- coding: utf-8 -*-
"""按账号隔离数据：**有主的只有主人看得见，无主的公共。看得见 = 能删。**

规则只写在 `app/services/ownership.py`，这里逐条验证它在每个读接口上都真的生效。
上一轮只有删除做了归属校验、读还是共享的；这个文件是那之后补的"读也收口"的护栏。

刻意不走上传/推理：任务、告警、地块都直接造行，毫秒级，也不会因为权重不在而跳过。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.models import (
    Alert,
    Device,
    DeviceStatus,
    Plot,
    Task,
    TaskMode,
    TaskStatus,
    User,
    utcnow,
)
from app.services.alerts import evaluate_task, _window_value

#: 无主的任务：本机摄像头抓拍写的就是 `user_id=NULL`
OWNERLESS = TaskMode.LOCAL


@pytest.fixture()
def user_a(register_user) -> dict[str, str]:
    return register_user("farmer")


@pytest.fixture()
def user_b(register_user) -> dict[str, str]:
    return register_user("neighbour")


def _uid(app, username: str) -> int:
    """按用户名取 id，不写死"第一个注册的就是 1"。"""
    session = app.state.session_factory()
    try:
        return session.query(User).filter(User.username == username).one().id
    finally:
        session.close()


def _make_task(
    app,
    user_id: int | None,
    *,
    unique: int = 0,
    plot_id: int | None = None,
    device_id: int | None = None,
    mode: str = TaskMode.IMAGE,
    counts: dict[str, int] | None = None,
) -> int:
    session = app.state.session_factory()
    try:
        task = Task(
            mode=mode,
            status=TaskStatus.DONE,
            source_name="sample.jpg",
            user_id=user_id,
            plot_id=plot_id,
            device_id=device_id,
            total_objects=unique,
            unique_objects=unique,
            counts_json=json.dumps(counts or {}, ensure_ascii=False),
            started_at=utcnow(),
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id
    finally:
        session.close()


def _make_alert(app, owner_id: int | None, *, rule: str = "测试规则") -> int:
    session = app.state.session_factory()
    try:
        alert = Alert(owner_id=owner_id, rule=rule, message="测试告警", level="warn")
        session.add(alert)
        session.commit()
        session.refresh(alert)
        return alert.id
    finally:
        session.close()


# ---------------------------------------------------------------- 任务
def test_a_foreign_task_is_invisible_on_every_reader(
    client: TestClient, app, user_a, user_b
):
    """所有读接口同一口径：不是自己的任务**在 B 眼里等于不存在**。

    逐个接口断言，而不是只测列表：只给列表加过滤的话，详情/明细/计数/报告
    仍然可以按 id 逐个取出来 —— "列表里没有"完全不妨碍别人记住任务号。
    """
    task_id = _make_task(app, _uid(app, "farmer"), unique=3)

    listed = client.get("/api/tasks", headers=user_b).json()
    assert listed["total"] == 0
    assert task_id not in {item["id"] for item in listed["items"]}

    for path in (
        f"/api/tasks/{task_id}",
        f"/api/tasks/{task_id}/detections",
        f"/api/tasks/{task_id}/counts",
        f"/api/tasks/{task_id}/report",
    ):
        assert client.get(path, headers=user_b).status_code == 404, path

    # 看得见 = 能删：不存在的东西自然也删不了
    assert client.delete(f"/api/tasks/{task_id}", headers=user_b).status_code == 404

    # 主人那边一切正常
    assert client.get(f"/api/tasks/{task_id}", headers=user_a).status_code == 200
    assert client.get("/api/tasks", headers=user_a).json()["total"] == 1


def test_an_ownerless_task_is_public(client: TestClient, app, user_a, user_b):
    """本机抓拍（`user_id=NULL`）是农场公共数据：两个账号都看得见、都能删。

    这条挡的是**过度收紧**。把规则写成 `user_id == 我` 的话，本机抓拍的那一大批
    会从所有人眼前消失，而且没人能删 —— 那是数据丢了，不是隔离做对了。
    """
    task_id = _make_task(app, None, mode=OWNERLESS, unique=5)

    for headers in (user_a, user_b):
        assert client.get(f"/api/tasks/{task_id}", headers=headers).status_code == 200
        assert client.get("/api/tasks", headers=headers).json()["total"] == 1

    assert client.delete(f"/api/tasks/{task_id}", headers=user_b).status_code == 204


# ---------------------------------------------------------------- 看板
def test_stats_only_sum_what_i_can_see(client: TestClient, app, user_a, user_b):
    """看板不是"所有人的虫子数之和" —— 那个数字会原样显示给两个账号。"""
    _make_task(app, _uid(app, "farmer"), unique=2)
    _make_task(app, _uid(app, "neighbour"), unique=3)
    _make_task(app, None, mode=OWNERLESS, unique=5)  # 公共数据，两边都算

    mine = client.get("/api/stats/summary", headers=user_a).json()
    theirs = client.get("/api/stats/summary", headers=user_b).json()

    assert mine["total_tasks"] == 2 and mine["unique_objects"] == 7
    assert theirs["total_tasks"] == 2 and theirs["unique_objects"] == 8


def test_plot_count_only_counts_visible_plots(client: TestClient, app, user_a, user_b):
    client.post("/api/plots", json={"name": "A 的地"}, headers=user_a)
    client.post("/api/plots", json={"name": "B 的地"}, headers=user_b)
    # 历史地块（迁移前的数据）owner_id 为 NULL，是公共的
    session = app.state.session_factory()
    try:
        session.add(Plot(name="老地块", owner_id=None))
        session.commit()
    finally:
        session.close()

    assert client.get("/api/stats/summary", headers=user_a).json()["plot_count"] == 2
    assert client.get("/api/stats/summary", headers=user_b).json()["plot_count"] == 2


# ---------------------------------------------------------------- 告警
def test_alerts_are_scoped_and_ack_all_does_not_touch_others(
    client: TestClient, app, user_a, user_b
):
    a_alert = _make_alert(app, _uid(app, "farmer"))
    b_alert = _make_alert(app, _uid(app, "neighbour"))
    public_alert = _make_alert(app, None)

    listed = {item["id"] for item in client.get("/api/alerts", headers=user_b).json()}
    assert b_alert in listed and public_alert in listed
    assert a_alert not in listed

    # 确认别人的告警 = 看不见 = 404
    assert client.post(f"/api/alerts/{a_alert}/ack", headers=user_b).status_code == 404

    # ack-all 只清"我看得见的"：自己的 + 无主的
    assert client.post("/api/alerts/ack-all", headers=user_b).json()["acked"] == 2

    session = app.state.session_factory()
    try:
        assert session.get(Alert, a_alert).acked is False, "不能顺手清掉别人的告警"
        assert session.get(Alert, b_alert).acked is True
        assert session.get(Alert, public_alert).acked is True
    finally:
        session.close()


def test_the_24h_window_does_not_include_another_account(app, user_a, user_b):
    """告警的 `value` 会进文案 —— 它必须是"这个任务的主人的历史"。

    这是**定量**泄漏：列表里少一条看得见，而一个偏大的数字没人能察觉。
    """
    _make_task(app, _uid(app, "farmer"), unique=200)
    _make_task(app, _uid(app, "neighbour"), unique=1)

    session = app.state.session_factory()
    try:
        task = (
            session.query(Task)
            .filter(Task.user_id == _uid(app, "farmer"))
            .one()
        )
        rule = {"metric": "unique_objects", "window": "24h"}
        assert _window_value(session, task, rule) == 200, "B 的 1 头不该被算进 A 的窗口"
    finally:
        session.close()


def test_the_dedup_window_is_per_owner(app, user_a, user_b):
    """两个账号各自触发同一条规则，都该收到告警。

    去重若不分归属，`plot_id` 为 NULL（人工上传的常态）时 A 会压掉 B 的告警
    整整 30 分钟，而且界面上没有任何迹象。
    """
    _make_task(app, _uid(app, "farmer"), unique=200)
    _make_task(app, _uid(app, "neighbour"), unique=200)

    session = app.state.session_factory()
    try:
        for user_id in (_uid(app, "farmer"), _uid(app, "neighbour")):
            task = session.query(Task).filter(Task.user_id == user_id).one()
            evaluate_task(session, task)
        count = (
            session.query(Alert)
            .filter(Alert.rule == "24 小时内累计偏高")
            .count()
        )
        assert count == 2, "两个账号应各有一条，而不是被对方的去重窗口压掉一条"
    finally:
        session.close()


# ---------------------------------------------------------------- 地块
def test_plots_are_owner_scoped(client: TestClient, app, user_a, user_b):
    a_plot = client.post("/api/plots", json={"name": "东头大棚"}, headers=user_a).json()
    b_plot = client.post("/api/plots", json={"name": "西头大棚"}, headers=user_b).json()

    assert [p["name"] for p in client.get("/api/plots", headers=user_b).json()] == ["西头大棚"]

    assert client.patch(
        f"/api/plots/{a_plot['id']}", json={"name": "改个名"}, headers=user_b
    ).status_code == 404
    assert client.delete(f"/api/plots/{a_plot['id']}", headers=user_b).status_code == 404

    # 自己的照常能删；A 的还在 A 的列表里
    assert client.delete(f"/api/plots/{b_plot['id']}", headers=user_b).status_code == 204
    assert [p["name"] for p in client.get("/api/plots", headers=user_a).json()] == ["东头大棚"]


def test_deleting_my_plot_does_not_touch_another_accounts_tasks(
    client: TestClient, app, user_a, user_b
):
    """可达世界里，引用我的私有地块的任务只可能属于我（见 routers/plots.py 的说明）。"""
    a_plot = client.post("/api/plots", json={"name": "A 的地"}, headers=user_a).json()
    b_plot = client.post("/api/plots", json={"name": "B 的地"}, headers=user_b).json()
    a_task = _make_task(app, _uid(app, "farmer"), unique=1, plot_id=a_plot["id"])
    b_task = _make_task(app, _uid(app, "neighbour"), unique=1, plot_id=b_plot["id"])

    assert client.delete(f"/api/plots/{b_plot['id']}", headers=user_b).status_code == 204

    session = app.state.session_factory()
    try:
        assert session.get(Task, b_task).plot_id is None
        # A 的地块还在，A 的任务不该被 B 的删除动作波及
        assert session.get(Task, a_task).plot_id == a_plot["id"]
    finally:
        session.close()


def test_a_device_cannot_be_attached_to_another_accounts_plot(
    client: TestClient, app, user_a, user_b
):
    """挂地块也要过归属校验：否则一台设备的上报会持续带上别人私有地块的 id。"""
    a_plot = client.post("/api/plots", json={"name": "A 的地"}, headers=user_a).json()

    started = client.post(
        "/api/devices/pair/start",
        json={"hardware_id": "pi-plot-guard", "name": "B 的盒子"},
    ).json()
    device = client.post(
        "/api/devices/bind", json={"code": started["code"]}, headers=user_b
    ).json()

    response = client.patch(
        f"/api/devices/{device['id']}", json={"plot_id": a_plot["id"]}, headers=user_b
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "地块不存在"


def test_device_results_do_not_follow_the_device_to_a_new_owner(
    client: TestClient, app, user_a, user_b
):
    """设备换主人后，旧主人的历史结果不能跟着设备一起交出去。

    解绑会保留历史任务（`user_id` 仍是 A），而设备行可以再被 B 绑定。
    `/devices/{id}/tasks` 只按 `device_id` 查的话，B 就能在这台设备的详情页里
    看到 A 的全部识别结果 —— 而且产物地址还会按 B 的身份签发。所以它必须和
    `/api/tasks` 一样再过一次任务归属。
    """
    started = client.post(
        "/api/devices/pair/start",
        json={"hardware_id": "pi-owner-change", "name": "换主的盒子"},
    ).json()
    device = client.post(
        "/api/devices/bind", json={"code": started["code"]}, headers=user_b
    ).json()

    a_task = _make_task(app, _uid(app, "farmer"), unique=1, device_id=device["id"])
    b_task = _make_task(app, _uid(app, "neighbour"), unique=1, device_id=device["id"])
    public_task = _make_task(app, None, mode=OWNERLESS, unique=1, device_id=device["id"])

    page = client.get(f"/api/devices/{device['id']}/tasks", headers=user_b).json()
    visible = {item["id"] for item in page["items"]}
    assert visible == {b_task, public_task}, "旧主人 A 的任务不该跟着设备过来"
    assert a_task not in visible
    # `total` 也是过滤后的数，否则翻页数字仍会泄露"还有多少条"
    assert page["total"] == 2

    # 设备本身只有 B 看得见：A 连这个接口都进不去
    assert client.get(f"/api/devices/{device['id']}/tasks", headers=user_a).status_code == 404
