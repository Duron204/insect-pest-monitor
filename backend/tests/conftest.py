# -*- coding: utf-8 -*-
"""pytest 公共夹具。

每个测试都拿到独立的临时数据库与输出目录，
绝不触碰真实的 backend/data/app.db。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import (  # noqa: E402
    AuthConfig,
    DatabaseConfig,
    Settings,
    load_settings,
)
from app.main import create_app  # noqa: E402
from app.routers import devices as devices_module  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    base = load_settings()
    return replace(
        base,
        data_dir=tmp_path / "data",
        # 测试里不挂前端静态目录
        server=replace(base.server, serve_frontend=False),
        database=DatabaseConfig(url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}"),
        storage=replace(
            base.storage,
            outputs_dir=str(tmp_path / "outputs"),
            uploads_dir=str(tmp_path / "uploads"),
        ),
        # 关掉预加载：否则每个用例都要加载一次模型，测试会慢到不可用。
        # 需要模型的用例自己在 test_detector.py 里显式加载。
        inference=replace(base.inference, preload=False),
        # 固定密钥（≥32 字节，避免触发 PyJWT 的密钥长度告警），
        # 也避免测试去写 .jwt_secret 文件
        auth=AuthConfig(
            jwt_secret="test-secret-key-for-pytest-only-0123456789", jwt_expire_hours=1
        ),
    )


@pytest.fixture()
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


#: `routers/devices.py` 里所有的进程内桶。**加新桶时必须补到这里** ——
#: 漏掉的症状是"单跑绿、连跑红"：前一个用例把某个 hardware_id 的计数打满，
#: 后一个用例（多半用同一个 HARDWARE_ID 常量）就凭空少了额度或凭空少了日志。
_DEVICE_BUCKETS = (
    devices_module._attempts_by_user,
    devices_module._pair_starts_by_hw,
    devices_module._poll_mismatch_logs,
)


@pytest.fixture(autouse=True)
def _reset_device_rate_limiters():
    """清空 `routers/devices.py` 里的进程内限流计数（`_DEVICE_BUCKETS`）。

    那几个 `deque` 字典是**模块级**的，而 `app` 夹具是每个用例新建的 ——
    清理必须挂在用例级别，否则同一台硬件跑到第 13 次 `pair/start` 就被自己的
    限流打成 429，症状是"单跑绿、连跑红"，最难查的那种。
    日志闸门同理，而且更隐蔽：它被前一个用例扣掉额度时，后一个用例断言
    "应该留下一条日志"就会看到零条，红得毫无线索。

    放在 conftest 而不是各个测试文件里：凡是碰配对的用例都得吃这套，
    复制到第三个文件时就该搬上来了。
    """
    for bucket in _DEVICE_BUCKETS:
        bucket.clear()
    yield
    for bucket in _DEVICE_BUCKETS:
        bucket.clear()


@pytest.fixture()
def register_user(client: TestClient):
    """注册并登录一个用户，返回可直接用的鉴权头。用户 id 从 1 开始按注册顺序分配。"""

    def _register(username: str) -> dict[str, str]:
        creds = {"username": username, "password": f"{username}123456"}
        client.post("/api/auth/register", json=creds)
        token = client.post("/api/auth/login", json=creds).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _register
