# -*- coding: utf-8 -*-
"""模型管理接口的测试。

重点覆盖一件此前漏掉的事：**注册表原先只扫 `.pt`，不认 `.onnx`**。
而树莓派上只部署 `.onnx`（没装 torch，跑不动 `.pt`）——
于是模型页在现场是空的。这里把"两种格式都要列出来"和
"在 torch 后端下选 .onnx 要给可读提示而不是底层报错"钉住。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ONNX_MODEL = PROJECT_ROOT / "weights" / "edge" / "best-yolo11s-640.onnx"

HAS_ONNX = ONNX_MODEL.is_file()
needs_onnx = pytest.mark.skipif(not HAS_ONNX, reason="本机没有导出的 ONNX 权重，跳过")


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    creds = {"username": "modeluser", "password": "modeluser12345"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_models_list_requires_login(client: TestClient):
    assert client.get("/api/models").status_code == 401


@needs_onnx
def test_onnx_edge_weight_is_listed(client: TestClient, auth_headers):
    payload = client.get("/api/models", headers=auth_headers).json()
    onnx = [item for item in payload["items"] if item["path"].endswith(".onnx")]
    assert onnx, "weights/edge 下的 .onnx 没出现在模型列表里（树莓派上模型页会是空的）"

    edge = next(item for item in onnx if item["source"] == "edge")
    assert edge["profile"] == "edge"
    # 边缘 ONNX 现在是从效果最好的 YOLO11s 导出的（此前是 yolov8n）
    assert edge["arch"] == "yolo11s", f"架构识别错了: {edge['arch']}"
    assert edge["exists"] is True


@needs_onnx
def test_selecting_onnx_under_torch_backend_gives_actionable_400(
    client: TestClient, auth_headers
):
    """开发机是 torch 后端，选 .onnx 必然失败 —— 但必须是**能照着做的**提示。

    （树莓派上 backend=onnx，同一个权重是可以正常切换的，所以这里不是"禁止"，
      而是"先告诉你要改什么"。）
    """
    payload = client.get("/api/models", headers=auth_headers).json()
    onnx = next(item for item in payload["items"] if item["path"].endswith(".onnx"))

    resp = client.post("/api/models/select", headers=auth_headers, json={"id": onnx["id"]})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "onnx" in detail.lower()
    assert "inference.backend" in detail


def test_selecting_unknown_model_returns_404(client: TestClient, auth_headers):
    resp = client.post("/api/models/select", headers=auth_headers, json={"id": 999999})
    assert resp.status_code == 404
