# -*- coding: utf-8 -*-
"""实时虫情上下文：把库里"此刻真发生的事"压成一小段提示词。

**为什么每次实时查库而不是做工具调用（function calling）**：
本系统的可问维度是固定的（近 7 天统计 / 告警 / 地块 / 设备，15 类），
一次预取摘要的 token 成本（<300）远低于让模型多轮决定"先查什么"的
延迟与失败面；同时模型拿到的永远是真数据 —— 编数字的口子从源头堵住。

归属口径与统计看板**逐字节一致**：`owner_scope`（无主公共 + 归我），
这里绝不另写一套过滤，否则"看板说 12 只、AI 说 20 只"会变成无解的悬案。
时间统一用 UTC 原样给出并注明 —— 前端聊天页与看板走的是本地时区，
混用会让"今天的虫"差八个小时。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Alert, Device, DeviceStatus, Plot, Task, TaskStatus, User, utcnow
from ..ownership import owner_scope
from ..taskmeta import load_counts

__all__ = ["DataSummary", "build_data_context", "collect_summary"]

_DAYS = 7
_TOP_CLASSES = 8
_TOP_ALERTS = 5


@dataclass(frozen=True)
class DataSummary:
    """实时虫情的结构化摘要。

    聊天（拼提示词）与诊断报告（挑重点虫种做知识检索）共用同一份
    数据口径 —— 两处各查一遍迟早查出两个不一样的数。
    """

    days: int
    tasks: int
    unique_objects: int
    total_objects: int
    top_classes: list[tuple[str, int]]
    alerts: list[Alert]
    plots: list[Plot]
    devices: list[Device]


def collect_summary(db: Session, user: User, days: int = _DAYS) -> DataSummary:
    """查库聚合。与统计看板同一过滤口径（owner_scope）。"""
    now = utcnow()
    since = now - timedelta(days=days)
    tasks = db.scalars(
        select(Task).where(
            Task.status == TaskStatus.DONE,
            Task.started_at >= since,
            owner_scope(Task.user_id, user),
        )
    ).all()

    by_class: dict[str, int] = defaultdict(int)
    today_key = now.strftime("%Y-%m-%d")
    today_unique = 0
    for task in tasks:
        if task.started_at is not None and task.started_at.strftime("%Y-%m-%d") == today_key:
            today_unique += task.unique_objects or 0
        for name, count in load_counts(task).items():
            by_class[name] += count

    return DataSummary(
        days=days,
        tasks=len(tasks),
        unique_objects=sum(task.unique_objects or 0 for task in tasks),
        total_objects=sum(task.total_objects or 0 for task in tasks),
        top_classes=sorted(by_class.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_CLASSES],
        alerts=list(
            db.scalars(
                select(Alert)
                .where(
                    Alert.acked.is_(False), Alert.ts >= since, owner_scope(Alert.owner_id, user)
                )
                .order_by(Alert.ts.desc())
                .limit(_TOP_ALERTS)
            )
        ),
        plots=list(
            db.scalars(
                select(Plot).where(owner_scope(Plot.owner_id, user)).order_by(Plot.id).limit(20)
            )
        ),
        devices=list(
            db.scalars(
                select(Device).where(
                    Device.status == DeviceStatus.ACTIVE, owner_scope(Device.owner_id, user)
                )
            )
        ),
    )


def build_data_context(db: Session, user: User, days: int = _DAYS) -> str:
    """生成注入提示词的实时数据段（纯文本，无 Markdown 表格 —— 表格费 token）。"""
    summary = collect_summary(db, user, days)
    now = utcnow()
    since = now - timedelta(days=days)

    class_line = "、".join(f"{name} {count}" for name, count in summary.top_classes) or "无"
    if summary.alerts:
        alert_lines = []
        for alert in summary.alerts:
            ts = alert.ts.strftime("%m-%d %H:%M") if alert.ts else "时间未知"
            alert_lines.append(f"- [{alert.level}] {alert.message}（UTC {ts}）")
        alert_block = "\n".join(alert_lines)
    else:
        alert_block = "- 近期无未确认告警"

    plot_line = (
        "、".join(
            f"{p.name}{'（' + p.crop + '）' if p.crop else ''}" for p in summary.plots
        )
        or "未登记地块"
    )
    device_line = (
        f"{len(summary.devices)} 台已绑定：" + "、".join(d.name or f"设备#{d.id}" for d in summary.devices)
        if summary.devices
        else "无已绑定设备"
    )

    # 明示"没有数据"：不写这句，模型会对着空上下文编一个像模像样的答案
    if summary.tasks == 0:
        stats_block = (
            f"近 {days} 天没有检测记录（任务数为 0）。\n"
            "回答涉及虫情时必须如实说明：还没有检测数据，建议先上传图片或让设备抓拍。"
        )
    else:
        today_unique = 0
        today_key = now.strftime("%Y-%m-%d")
        for task in db.scalars(
            select(Task).where(
                Task.status == TaskStatus.DONE,
                Task.started_at >= since,
                owner_scope(Task.user_id, user),
            )
        ).all():
            if task.started_at is not None and task.started_at.strftime("%Y-%m-%d") == today_key:
                today_unique += task.unique_objects or 0
        stats_block = (
            f"- 检测任务：{summary.tasks} 个（{since.strftime('%m-%d')} ~ {now.strftime('%m-%d')}，UTC）\n"
            f"- 累计检出（跟踪去重）：{summary.unique_objects} 只；原始框数：{summary.total_objects}\n"
            f"- 今日（UTC {today_key}）检出：{today_unique} 只\n"
            f"- 类别分布（去重）：{class_line}"
        )

    return (
        f"【实时虫情数据 · 时间均为 UTC，数据截至 {now.strftime('%Y-%m-%d %H:%M')}】\n"
        f"{stats_block}\n"
        f"【未确认告警（近 {days} 天，最多 {_TOP_ALERTS} 条）】\n{alert_block}\n"
        f"【地块】{plot_line}\n"
        f"【设备】{device_line}"
    )
