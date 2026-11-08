# -*- coding: utf-8 -*-
"""虫害知识库：从 `app/data/species.json` 播种到数据库。

保留 JSON 作为**唯一编辑入口**（便于手工维护/版本对比），
数据库只作为可查询的副本 —— 这样统计接口才能按类别直接 join。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from ..models import Species

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


@lru_cache(maxsize=1)
def load_species_payload() -> dict[str, Any]:
    path = _DATA_DIR / "species.json"
    if not path.exists():
        return {"species": [], "disclaimer": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"species": [], "disclaimer": []}


def species_list() -> list[dict[str, Any]]:
    return list(load_species_payload().get("species", []))


def seed_species(db: OrmSession) -> int:
    """把 JSON 播种进库（幂等）。返回处理条数。"""
    rows = species_list()
    if not rows:
        return 0

    existing = {row.class_id: row for row in db.scalars(select(Species)).all()}
    for item in rows:
        try:
            class_id = int(item["class_id"])
        except (KeyError, TypeError, ValueError):
            continue
        row = existing.get(class_id)
        if row is None:
            row = Species(class_id=class_id, en_name=str(item.get("en_name", "")), zh_name=str(item.get("zh_name", "")))
            db.add(row)
            existing[class_id] = row
        row.en_name = str(item.get("en_name", row.en_name))
        row.zh_name = str(item.get("zh_name", row.zh_name))
        row.host_crops = item.get("host_crops")
        row.damage = item.get("damage")
        row.control = item.get("control")
        row.threshold_desc = item.get("threshold_desc")
        row.image_path = item.get("image_path")
    db.commit()
    return len(rows)


def zh_to_en_map() -> dict[str, str]:
    """中文名 -> 英文名。用于把按中文名聚合的统计补上英文名。"""
    return {
        str(item.get("zh_name")): str(item.get("en_name"))
        for item in species_list()
        if item.get("zh_name") and item.get("en_name")
    }
