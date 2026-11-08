# -*- coding: utf-8 -*-
"""告警规则评估。

规则写在 `app/data/alert_rules.yaml`，每次任务完成后评估一次。
刻意保持简单与可读：农业现场需要的是「看得懂、改得动」的规则，
而不是一套通用表达式引擎。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session as OrmSession

from ..models import Alert, AlertLevel, Task, TaskStatus, utcnow
from .taskmeta import load_counts

logger = logging.getLogger("insect.alerts")
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_RULES_FILE = _DATA_DIR / "alert_rules.yaml"


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    if not _RULES_FILE.exists():
        return {"rules": [], "webhook": {"enabled": False}}
    try:
        payload = yaml.safe_load(_RULES_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("读取告警规则失败：%s", exc)
        return {"rules": [], "webhook": {"enabled": False}}
    if not isinstance(payload, dict):
        return {"rules": [], "webhook": {"enabled": False}}
    payload.setdefault("rules", [])
    payload.setdefault("webhook", {"enabled": False})
    return payload


def _window_value(
    db: OrmSession,
    task: Task,
    rule: dict[str, Any],
) -> float:
    """按规则的 metric/window/class_name 计算用于比较的数值。"""
    metric = str(rule.get("metric", "unique_objects"))
    window = str(rule.get("window", "本次任务"))
    class_name = rule.get("class_name")
    plot_id = rule.get("plot_id")

    lookback = None
    if window == "24h":
        lookback = utcnow() - timedelta(hours=24)
    elif window == "7d":
        lookback = utcnow() - timedelta(days=7)

    if lookback is None:
        # 本次任务：直接取任务的汇总字段
        if class_name:
            return float(load_counts(task).get(str(class_name), 0))
        return float(task.unique_objects if metric == "unique_objects" else task.total_objects)

    query = db.query(Task).filter(
        Task.status == TaskStatus.DONE, Task.started_at >= lookback
    )
    # 窗口 = **这个任务的主人的历史**，不是全库。
    # 不加这一句的话，24h/7d 规则算出来的是所有人的虫子数之和，而这个数字会
    # 原样进告警文案（`{value}`）—— A 的告警里含着 B 的数据。那是**定量**泄漏，
    # 比"列表里多看见一条"更难被发现，也更严重。
    # 无主的任务（本机抓拍）看无主的窗口，与"无主 = 公共"同一口径。
    if task.user_id is None:
        query = query.filter(Task.user_id.is_(None))
    else:
        query = query.filter(Task.user_id == task.user_id)
    if plot_id is not None:
        query = query.filter(Task.plot_id == plot_id)
    total = 0.0
    for row in query.all():
        if class_name:
            total += float(load_counts(row).get(str(class_name), 0))
        else:
            total += float(row.unique_objects if metric == "unique_objects" else row.total_objects)
    return total


def evaluate_task(db: OrmSession, task: Task) -> list[Alert]:
    """对单个任务评估全部规则，返回新产生的告警（已入库）。"""
    if task.status != TaskStatus.DONE:
        return []

    created: list[Alert] = []
    for rule in load_rules().get("rules", []):
        if not isinstance(rule, dict):
            continue
        try:
            threshold = float(rule.get("threshold", 0))
        except (TypeError, ValueError):
            continue

        rule_plot = rule.get("plot_id")
        if rule_plot is not None and task.plot_id != rule_plot:
            continue

        try:
            value = _window_value(db, task, rule)
        except Exception as exc:  # noqa: BLE001 - 单条规则失败不应影响其它规则
            logger.warning("规则 %s 评估失败：%s", rule.get("name"), exc)
            continue

        if value < threshold:
            continue

        # 简单去重：同一规则在同一地块上 30 分钟内只报一次。
        # 否则「24 小时累计」这类规则会在每个新任务上都刷一条，很快就没人看告警了。
        #
        # **必须按归属分**：`plot_id` 常常是 NULL（人工上传的任务没有地块），
        # 不分开的话 A 的告警会把 B 的同一规则压掉 30 分钟 —— 两个账号各看各的
        # 数据，却共用一个去重窗口，B 会平白少一条告警，而且没有任何迹象。
        owner_filter = (
            Alert.owner_id.is_(None)
            if task.user_id is None
            else Alert.owner_id == task.user_id
        )
        recent = (
            db.query(Alert)
            .filter(
                Alert.rule == str(rule.get("name")),
                Alert.plot_id == task.plot_id,
                owner_filter,
                Alert.ts >= utcnow() - timedelta(minutes=30),
            )
            .first()
        )
        if recent is not None:
            continue

        level = str(rule.get("level", AlertLevel.INFO))
        if level not in AlertLevel.ALL:
            level = AlertLevel.INFO
        template = rule.get("message") or f"{rule.get('name', '规则命中')}：{value:.0f}"
        message = template.replace("{value}", f"{value:.0f}")

        alert = Alert(
            # 告警的归属 = 触发它的任务的主人（可能是 NULL = 公共）。
            # 冗余这一列而不是靠 task_id 现查的理由见 models.py 里 Alert.owner_id。
            owner_id=task.user_id,
            plot_id=task.plot_id,
            task_id=task.id,
            level=level,
            rule=str(rule.get("name") or "未命名规则"),
            message=message,
            value=value,
            ts=utcnow(),
        )
        db.add(alert)
        created.append(alert)

    if created:
        db.commit()
    return created
