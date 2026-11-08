# -*- coding: utf-8 -*-
"""宿主身份识别的测试。

`describe_host()` 决定设置页「本机摄像头」面板怎么说明"本机"是谁 ——
现场部署时后端在树莓派上，开发机上则在开发机自己。判断错了界面就会误导人。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.hostinfo import describe_host


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    creds = {"username": "hostuser", "password": "hostuser12345"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_detects_raspberry_pi_from_device_tree_model(tmp_path: Path):
    model_dir = tmp_path / "device-tree"
    model_dir.mkdir()
    (model_dir / "model").write_text("Raspberry Pi 5 Model B Rev 1.0\x00", encoding="utf-8")

    host = describe_host(tmp_path)
    assert host["is_raspberry_pi"] is True
    # 尾部 NUL 要去掉（树莓派上这个文件就是 NUL 结尾的）
    assert host["model"] == "Raspberry Pi 5 Model B Rev 1.0"
    assert host["platform"]
    assert host["machine"]


def test_other_arm_board_is_not_reported_as_pi(tmp_path: Path):
    """别的 ARM 板子不能冒充树莓派 —— 只有 model 里明确写着 Raspberry Pi 才算。"""
    model_dir = tmp_path / "device-tree"
    model_dir.mkdir()
    (model_dir / "model").write_text("Rockchip RK3588 EVB", encoding="utf-8")

    host = describe_host(tmp_path)
    assert host["is_raspberry_pi"] is False
    assert host["model"] == "Rockchip RK3588 EVB"


def test_missing_model_file_is_not_an_error(tmp_path: Path):
    """开发机（Windows/无 /proc）上不应报错，如实返回非树莓派。"""
    host = describe_host(tmp_path)
    assert host["is_raspberry_pi"] is False
    assert host["model"] is None
    assert host["platform"]


def test_status_endpoint_exposes_host(client: TestClient, auth_headers):
    """状态接口要带上宿主信息，否则前端无从说明"本机"是谁。"""
    payload = client.get("/api/local-camera/status", headers=auth_headers).json()
    assert payload.get("host") is not None
    assert payload["host"]["is_raspberry_pi"] in (True, False)
    assert payload["host"]["platform"]
