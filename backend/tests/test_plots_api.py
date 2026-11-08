# -*- coding: utf-8 -*-
"""地块改名：**全场同名**规则在 PATCH 上也要生效。

`plots.name` 有唯一约束、`create_plot` 也查了重名，但 `update_plot` 早先直接
赋值 —— 改成一个已存在的名字会撞 `IntegrityError`，用户拿到的是 500。
这个文件钉住那条路径：重名返回 409，而不是"服务器坏了"。
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _create(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post("/api/plots", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_rename_to_an_existing_name_is_409(client: TestClient, register_user):
    headers = register_user("plotter")
    first = _create(client, headers, "东头")
    second = _create(client, headers, "西头")

    response = client.patch(
        f"/api/plots/{second['id']}", json={"name": "东头"}, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "同名地块已存在"

    # 改名失败不能留下半截状态
    names = [item["name"] for item in client.get("/api/plots", headers=headers).json()]
    assert names == ["东头", "西头"]


def test_rename_to_the_same_name_is_allowed(client: TestClient, register_user):
    """只改作物、名字原样提交，不该被自己拦住。"""
    headers = register_user("plotter")
    plot = _create(client, headers, "东头")

    response = client.patch(
        f"/api/plots/{plot['id']}",
        json={"name": "东头", "crop": "水稻"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["crop"] == "水稻"
