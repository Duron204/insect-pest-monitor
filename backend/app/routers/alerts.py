# -*- coding: utf-8 -*-
"""告警中心：列表、确认、以及查看当前生效的规则。

规则本身在 `app/data/alert_rules.yaml`，前端可以看到但不在网页上改 ——
阈值属于生产参数，让它在文件里可审计、可版本对比，比放在界面里更稳妥。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db
from ..models import Alert, User, utcnow
from ..schemas import AlertOut
from ..services.alerts import load_rules
from ..services.ownership import owned_or_404, owner_scope

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
def list_alerts(
    acked: bool | None = Query(None, description="是否已确认；不传表示全部"),
    level: str | None = Query(None, description="info|warn|critical"),
    plot_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AlertOut]:
    # 与任务同一口径（services/ownership.py）：告警的 owner 是触发它的任务的主人。
    # 无主的告警（本机抓拍触发、以及更早没有 owner_id 的历史数据）所有人可见。
    query = db.query(Alert).filter(owner_scope(Alert.owner_id, user))
    if acked is not None:
        query = query.filter(Alert.acked == acked)
    if level:
        query = query.filter(Alert.level == level)
    if plot_id is not None:
        query = query.filter(Alert.plot_id == plot_id)
    rows = query.order_by(Alert.ts.desc(), Alert.id.desc()).limit(limit).all()
    return [AlertOut.model_validate(row) for row in rows]


@router.get("/rules")
def get_rules(_: User = Depends(get_current_user)) -> dict:
    """回显当前告警规则，便于前端解释「为什么会报警」。"""
    return load_rules()


@router.post("/{alert_id}/ack", response_model=AlertOut)
def ack_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AlertOut:
    # 看不见的告警 = 不存在（404）。确认别人的告警本来也没什么意义，
    # 但更重要的是别让它成为一个按 id 试探"这条告警归谁"的接口。
    alert = owned_or_404(
        db, Alert, alert_id, user, owner_attr="owner_id", what="告警"
    )
    alert.acked = True
    alert.ts = alert.ts or utcnow()
    db.commit()
    db.refresh(alert)
    return AlertOut.model_validate(alert)


@router.post("/ack-all")
def ack_all(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """把**我看得见的**未确认告警全部标为已读。

    加 `owner_scope` 之前这一句是"把全库所有账号的告警都标已读" —— 一个账号点
    一次"全部确认"，别人的告警中心就一起清空了。无主的告警仍算公共，谁都能确认，
    与删除任务同一口径。
    """
    updated = (
        db.query(Alert)
        .filter(Alert.acked.is_(False), owner_scope(Alert.owner_id, user))
        .update({Alert.acked: True}, synchronize_session=False)
    )
    db.commit()
    return {"acked": int(updated)}
