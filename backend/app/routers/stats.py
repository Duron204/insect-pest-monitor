# -*- coding: utf-8 -*-
"""统计接口：为前端看板提供聚合数据。

口径说明（这是最容易出错的地方）：
- `total_objects` = 框数总和（同一只虫在连续帧会被重复计）
- `unique_objects` = 跟踪去重后的虫数总和 —— **对外展示应以它为准**
- 分类计数从 `tasks.counts_json` 聚合，而不是扫描 detections 明细：
  明细对长视频是抽样入库的，扫描它会低估数量。

**时间分桶（`by_day` / `by_hour`）必须按调用方的本地时间，不能按 UTC。**
库里统一存裸 UTC（见 models.py 的模块头），而前端把这个接口的 `by_hour` 直接
当作"0-23 时"画在横轴上、把 `by_day` 当作日期标签画 —— 拿 UTC 去分桶，
在 UTC+8 上"峰值 03:00"其实是上午 11 点，凌晨的抓拍还会被算进**前一天**。
这类错误不会报错，只会让人对着图表得出相反的结论。
分桶后的计数前端没法自己换算（它拿到的是 24 个数字，不是 24 个时间戳），
所以偏移量只能由调用方传进来：见 `summary` 的 `tz_offset_min`。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db
from ..models import Device, DeviceStatus, Plot, Task, TaskStatus, User, utcnow
from ..schemas import ClassCountOut, DayCountOut, StatsSummaryOut
from ..services.knowledge import zh_to_en_map
from ..services.ownership import owner_scope
from ..services.taskmeta import load_counts

router = APIRouter(prefix="/stats", tags=["stats"])


def _resolve_since(range_token: str):
    """把 7d / 30d / 24h / all 解析成起始时间；无法识别时按 7 天处理。"""
    token = (range_token or "7d").strip().lower()
    now = utcnow()
    if token == "all":
        return None
    try:
        if token.endswith("d"):
            return now - timedelta(days=int(token[:-1]))
        if token.endswith("h"):
            return now - timedelta(hours=int(token[:-1]))
    except ValueError:
        pass
    return now - timedelta(days=7)


@router.get("/summary", response_model=StatsSummaryOut)
def summary(
    range_token: str = Query("7d", alias="range"),
    plot_id: int | None = Query(None),
    device_id: int | None = Query(None, description="只看某台绑定设备的结果（需归你所有）"),
    tz_offset_min: int = Query(
        0,
        ge=-1440,
        le=1440,
        description="调用方相对 UTC 的分钟偏移（UTC+8 传 480）。只影响 by_day / by_hour 的分桶。",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StatsSummaryOut:
    since = _resolve_since(range_token)
    # 时间分桶用的偏移。**默认 0 = 逐字节保持原来的行为**（按 UTC 分桶），
    # 所以不传这个参数的老前端不会被这次改动影响。
    # `since` 不受它影响：那是一个"截至此刻的滑动窗口"（近 7 天 = 最近 168 小时），
    # 与观察者在哪个时区无关。
    local_shift = timedelta(minutes=tz_offset_min)

    # 看板与历史页同一口径：无主的（本机抓拍、旧数据）公共，有主的只有主人看得见。
    # 少了这一句，看板的数字就会是"所有人的虫子数之和" —— 比列表泄漏更隐蔽，
    # 因为没人能从一个数字里看出它算多了。
    query = db.query(Task).filter(
        Task.status == TaskStatus.DONE, owner_scope(Task.user_id, user)
    )
    if since is not None:
        query = query.filter(Task.started_at >= since)
    if plot_id is not None:
        query = query.filter(Task.plot_id == plot_id)
    if device_id is not None:
        # 与 /api/tasks 的 device_id 过滤同一口径：先确认这台设备归你，再过滤。
        # 加这个参数是为了让设备详情页能零新增聚合代码地复用看板的折线图。
        owned = db.get(Device, device_id)
        if owned is None or owned.owner_id != user.id or owned.status != DeviceStatus.ACTIVE:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
        query = query.filter(Task.device_id == device_id)
    tasks = query.all()

    total_objects = 0
    unique_objects = 0
    by_class: dict[str, int] = defaultdict(int)
    by_day: dict[str, int] = defaultdict(int)
    by_hour = [0] * 24

    for task in tasks:
        total_objects += task.total_objects or 0
        unique_objects += task.unique_objects or 0
        for name, count in load_counts(task).items():
            by_class[name] += count
        if task.started_at is not None:
            # 库里的 started_at 是裸 UTC，加偏移才是观察者看到的那个"几点、哪一天"
            local = task.started_at + local_shift
            by_day[local.strftime("%Y-%m-%d")] += task.unique_objects or 0
            by_hour[local.hour] += 1

    zh_to_en = zh_to_en_map()
    class_items = [
        ClassCountOut(zh_name=name, en_name=zh_to_en.get(name), count=count)
        for name, count in sorted(by_class.items(), key=lambda item: -item[1])
        if count > 0
    ]

    return StatsSummaryOut(
        range=range_token,
        total_tasks=len(tasks),
        total_objects=total_objects,
        unique_objects=unique_objects,
        by_class=class_items,
        by_day=[DayCountOut(date=day, count=count) for day, count in sorted(by_day.items())],
        by_hour=by_hour,
        # 地块数也要按同一口径数：历史地块 owner_id 为 NULL（公共），
        # 所以升级后这个数字不会突然变小；只有别人自己新画的地块不进来。
        plot_count=db.query(Plot).filter(owner_scope(Plot.owner_id, user)).count(),
    )
