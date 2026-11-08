# -*- coding: utf-8 -*-
"""虫害知识库接口（15 类的危害特征 / 寄主 / 防治建议 / 阈值 + 参考图）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_current_user, get_db
from ..models import Species, User
from ..schemas import SpeciesOut
from ..services.knowledge import load_species_payload, seed_species
from ..services.species_samples import sample_for, samples_for

router = APIRouter(prefix="/species", tags=["species"])


@router.get("", response_model=list[SpeciesOut])
def list_species(
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[SpeciesOut]:
    seed_species(db)  # 幂等：JSON 是唯一编辑入口，库里只是可查询副本
    rows = db.scalars(select(Species).order_by(Species.class_id)).all()

    # 有数据集样本图的类别，把 image_path 指向下面的 sample 接口；
    # 没有就保持 None，前端会退回"暂无参考图"占位。
    settings: Settings = request.app.state.settings
    available = samples_for(settings)
    items: list[SpeciesOut] = []
    for row in rows:
        out = SpeciesOut.model_validate(row)
        if row.class_id in available:
            out = out.model_copy(update={"image_path": f"/api/species/sample/{row.class_id}"})
        items.append(out)
    return items


@router.get("/sample/{class_id}")
def get_sample_image(class_id: int, request: Request) -> FileResponse:
    """某个类别的数据集实拍图（知识库卡片配图）。

    **刻意不要求登录**：卡片里是 `<img src=...>`，浏览器不会附带 Authorization 头，
    加鉴权只会让图片 404。这与标注图挂在 `/media/...` 上的匿名策略一致
    （本机单用户部署，图片本身不含敏感信息）。
    """
    settings: Settings = request.app.state.settings
    path = sample_for(settings, class_id)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该类暂无参考图（本机没有数据集？）",
        )
    return FileResponse(
        path,
        headers={"Cache-Control": "public, max-age=86400"},
        filename=path.name,
    )


@router.get("/disclaimer")
def get_disclaimer(_: User = Depends(get_current_user)) -> dict:
    """防治建议的使用声明 —— 前端需在知识卡上展示。"""
    payload = load_species_payload()
    return {"disclaimer": payload.get("disclaimer", [])}
