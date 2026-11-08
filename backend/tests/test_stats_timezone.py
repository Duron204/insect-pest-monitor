# -*- coding: utf-8 -*-
"""`/api/stats/summary` 的时间分桶：必须按**观察者的本地时间**，不是 UTC。

为什么值得单独一个文件：库里统一存裸 UTC（见 models.py 的模块头），而前端把
`by_hour` 直接当作"0-23 时"画在横轴上、把 `by_day` 当作日期标签画。拿 UTC 去分桶，
在 UTC+8 上"峰值 03:00"其实是上午 11 点，凌晨的抓拍还会被算进**前一天**。
这类错误不报错、不崩溃，只是让人对着图表得出相反的结论 —— 所以必须有用例钉住。

分桶后的计数前端没法自己换算（它拿到的是 24 个数字，不是 24 个时间戳），
所以偏移量由调用方通过 `tz_offset_min` 传进来。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Task, TaskMode, TaskStatus

#: 这一条任务落在 UTC 的 23:30 —— 刻意贴着整日边界，
#: 好让"分桶用哪个时区"这件事在 `by_day` 上也能看出来（不只是小时数不同）。
CROSS_MIDNIGHT_UTC = datetime(2026, 10, 15, 23, 30, 0)


@pytest.fixture()
def viewer(register_user) -> dict[str, str]:
    return register_user("viewer")


def _add_task(session_factory, started_at: datetime, unique_objects: int = 0) -> int:
    """插一条 DONE 任务。只填分桶用得上的字段，别的吃模型默认值。"""
    db: Session = session_factory()
    try:
        task = Task(
            mode=TaskMode.IMAGE,
            status=TaskStatus.DONE,
            started_at=started_at,
            unique_objects=unique_objects,
            total_objects=unique_objects,
            frame_count=1,
        )
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _summary(client: TestClient, headers: dict[str, str], **params) -> dict:
    params.setdefault("range", "all")
    response = client.get("/api/stats/summary", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_hour_and_day_follow_the_offset(client: TestClient, app, viewer):
    """UTC 23:30 在 UTC+8 是**次日** 07:30。

    这条同时钉住两件事：小时数要挪，日期也要跟着跨过去 ——
    只挪小时不挪日期的话，凌晨那几小时的抓拍会被算到前一天头上，
    而那正是"昨天没虫、今天也没虫"这类错判的来源。
    """
    _add_task(app.state.session_factory, CROSS_MIDNIGHT_UTC, unique_objects=5)

    local = _summary(client, viewer, tz_offset_min=480)
    assert local["by_hour"][7] == 1
    assert local["by_hour"][23] == 0, "按 UTC 分桶的结果不该还留在里面"
    assert [(d["date"], d["count"]) for d in local["by_day"]] == [("2026-10-16", 5)]


def test_default_offset_is_utc(client: TestClient, app, viewer):
    """不传 `tz_offset_min` 就是原来的行为。

    这条是**向后兼容的护栏**：老前端（或任何第三方调用方）不传这个参数时，
    数字必须与这次改动之前逐字节一致 —— 否则一次"默认值写错"就能让
    所有现存客户端的图表悄悄偏掉。
    """
    _add_task(app.state.session_factory, CROSS_MIDNIGHT_UTC, unique_objects=5)

    body = _summary(client, viewer)
    assert body["by_hour"][23] == 1
    assert [(d["date"], d["count"]) for d in body["by_day"]] == [("2026-10-15", 5)]


def test_negative_offset_shifts_backwards(client: TestClient, app, viewer):
    """UTC-8：UTC 23:30 是**当天** 15:30。

    专门盯符号。后端要的是"UTC + offset = 本地"，而浏览器的
    `getTimezoneOffset()` 给的是"本地比 UTC 慢多少分钟"（UTC+8 → -480），
    两者差一个负号。前端那边漏掉负号会让偏差**翻倍**，
    这条用例保证后端这一侧对负偏移的理解没有跟着一起歪。
    """
    _add_task(app.state.session_factory, CROSS_MIDNIGHT_UTC, unique_objects=5)

    body = _summary(client, viewer, tz_offset_min=-480)
    assert body["by_hour"][15] == 1
    assert [(d["date"], d["count"]) for d in body["by_day"]] == [("2026-10-15", 5)]


def test_offset_only_moves_the_buckets_not_the_totals(client: TestClient, app, viewer):
    """偏移只该改变"落在哪个格子"，不该改变"一共有多少"。

    这是最容易在改动里被顺手弄坏的守恒量：一旦分桶写成"只对某个偏移下的
    任务计数"，总数就会跟着偏移量变，而总数恰恰是看板上最显眼的那个数字。
    """
    session_factory = app.state.session_factory
    _add_task(session_factory, CROSS_MIDNIGHT_UTC, unique_objects=5)
    _add_task(session_factory, datetime(2026, 10, 15, 1, 0, 0), unique_objects=3)

    for offset in (0, 480, -480):
        body = _summary(client, viewer, tz_offset_min=offset)
        assert body["total_tasks"] == 2, offset
        assert body["unique_objects"] == 8, offset
        assert sum(body["by_hour"]) == 2, offset
        assert sum(d["count"] for d in body["by_day"]) == 8, offset


def test_absurd_offset_is_rejected(client: TestClient, viewer):
    """超出 ±1440 分钟的偏移直接 422，而不是算出一个不存在的日期。"""
    response = client.get(
        "/api/stats/summary", params={"range": "all", "tz_offset_min": 100000}, headers=viewer
    )
    assert response.status_code == 422
