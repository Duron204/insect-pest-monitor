# -*- coding: utf-8 -*-
"""地块管理。农业语义的核心维度：任务、设备、告警都归属地块。

保留它不只是为了分组 —— 看板的「按地块对比」与告警阈值都依赖这一维度。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db
from ..models import Plot, Task, User
from ..schemas import PlotIn, PlotOut
from ..services.ownership import owned_or_404, owner_scope

router = APIRouter(prefix="/plots", tags=["plots"])


@router.get("", response_model=list[PlotOut])
def list_plots(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PlotOut]:
    # 无主的（历史地块）公共，自己建的只有自己看得见。见 services/ownership.py。
    rows = db.scalars(
        select(Plot).where(owner_scope(Plot.owner_id, user)).order_by(Plot.id)
    ).all()
    return [PlotOut.model_validate(row) for row in rows]


@router.post("", response_model=PlotOut, status_code=status.HTTP_201_CREATED)
def create_plot(
    payload: PlotIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlotOut:
    # 同名检查是**全场**的，不是按账号分：地块是物理世界里的一件东西，
    # 两个账号不该各建一个"东头"（见 models.py 里 Plot.owner_id 的注释）。
    exists = db.scalar(select(Plot).where(Plot.name == payload.name))
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同名地块已存在")
    plot = Plot(**payload.model_dump(), owner_id=user.id)
    db.add(plot)
    db.commit()
    db.refresh(plot)
    return PlotOut.model_validate(plot)


@router.patch("/{plot_id}", response_model=PlotOut)
def update_plot(
    plot_id: int,
    payload: PlotIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlotOut:
    plot = owned_or_404(db, Plot, plot_id, user, owner_attr="owner_id", what="地块")
    # 与 create_plot 同一条「全场同名」规则（见 models.py 的说明）。
    # 少了这一句，PATCH 撞上唯一约束就是 `IntegrityError` → 500 —— 用户以为
    # 服务出了故障，实际只是名字重复。排除自己，否则"只改作物不改名"也会被拦。
    if payload.name != plot.name:
        exists = db.scalar(
            select(Plot).where(Plot.name == payload.name, Plot.id != plot.id)
        )
        if exists is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="同名地块已存在"
            )
    for key, value in payload.model_dump().items():
        setattr(plot, key, value)
    db.commit()
    db.refresh(plot)
    return PlotOut.model_validate(plot)


@router.delete("/{plot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plot(
    plot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    plot = owned_or_404(db, Plot, plot_id, user, owner_attr="owner_id", what="地块")

    # 不删除历史任务，只解除归属关系（外键为 SET NULL），避免误删数据。
    # **只解除调用者看得见的那些任务** —— 跨账号写在这里不只是失礼，它会静默改掉
    # 别人历史记录上的地块。
    #
    # 诚实说明：`tasks.plot_id` 的外键本身是 `ON DELETE SET NULL`，最后那行
    # `db.delete(plot)` 由 SQLite 处理时，仍会清掉**所有**引用它的任务。之所以
    # 依然安全，是因为"别人的任务挂在我的私有地块上"这条状态通过接口造不出来：
    # 人工上传不写 plot_id，设备挂地块要过归属校验（routers/devices.py），
    # 于是可达世界里引用某块私有地的任务只属于它主人。无主地块是公共的，
    # 删掉它时所有人一起解除引用，正是"无主 = 公共"该有的样子。
    # 这行显式 update 保证即使将来换掉/去掉外键，语义也不变。
    db.query(Task).filter(
        Task.plot_id == plot_id, owner_scope(Task.user_id, user)
    ).update({Task.plot_id: None}, synchronize_session=False)
    db.delete(plot)
    db.commit()
