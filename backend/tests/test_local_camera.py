# -*- coding: utf-8 -*-
"""本机（树莓派直连）摄像头采集的测试。

用 `simulated` 图像源 —— 它读 `test_media/` 下的真实图片，因此整条链路
（取图 → 推理 → 落库 → 历史/详情/标注图）都是真跑的，不是打桩。
这正是设计这个图像源的目的：没有摄像头硬件也能把功能验证到可交付。
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services import local_camera as local_camera_module

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEIGHTS = PROJECT_ROOT / "weights" / "best-yolo11s.pt"
#: 权重被 .gitignore 排除（18MB 二进制不进仓库），因此 CI 上没有它。
#: 这种情况**显式跳过**并在报告里写清原因，而不是让 CI 无故失败。
pytestmark = pytest.mark.skipif(
    not DEFAULT_WEIGHTS.is_file(), reason=f"缺少权重 {DEFAULT_WEIGHTS.name}（仓库不存二进制）"
)

# ---------------------------------------------------------------- 夹具
@pytest.fixture()
def local_settings(settings: Settings, tmp_path: Path) -> Settings:
    """模拟图像源 + 不自动启动（由各用例显式 start）。"""
    return replace(
        settings,
        local_camera=replace(
            settings.local_camera,
            enabled=False,
            backend="simulated",
            interval_min=30,
            conf=0.25,
            iou=0.5,
        ),
    )


@pytest.fixture()
def local_app(local_settings: Settings):
    app = create_app(local_settings)
    # 抓拍要真跑推理，所以这里显式加载权重（conftest 默认关掉预加载是为了速度）
    app.state.detector.load(str(DEFAULT_WEIGHTS))
    return app


@pytest.fixture()
def local_client(local_app):
    with TestClient(local_app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(local_client: TestClient) -> dict[str, str]:
    creds = {"username": "localcam", "password": "localcam123"}
    local_client.post("/api/auth/register", json=creds)
    token = local_client.post("/api/auth/login", json=creds).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 鉴权与状态
def test_status_requires_auth(local_client: TestClient):
    assert local_client.get("/api/local-camera/status").status_code == 401
    assert local_client.post("/api/local-camera/capture").status_code == 401


def test_status_lists_sources_with_reasons(local_client: TestClient, auth_headers):
    """不可用的图像源要给出原因和安装命令，否则现场只能靠猜。"""
    response = local_client.get("/api/local-camera/status", headers=auth_headers)
    assert response.status_code == 200
    status = response.json()

    assert status["backend"] == "simulated"
    assert status["enabled"] is False
    assert status["running"] is False

    by_name = {entry["backend"]: entry for entry in status["available"]}
    assert {"picamera2", "opencv", "simulated"} <= set(by_name)
    assert by_name["simulated"]["available"] is True
    # 本机没有 picamera2 时应明确说明并给出安装命令（有的话这条不成立，跳过）
    if not by_name["picamera2"]["available"]:
        assert "python3-picamera2" in by_name["picamera2"]["detail"]


# ---------------------------------------------------------------- 手动抓拍
def test_manual_capture_creates_local_task_visible_in_history(
    local_client: TestClient, auth_headers
):
    """抓一拍 → 任务进历史 → 详情有计数与标注图。这是本机取图的核心价值。"""
    response = local_client.post("/api/local-camera/capture", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trigger"] == "manual"
    assert body["backend"] == "simulated"
    task_id = body["task_id"]

    # 历史里按 mode=local 能查到
    listing = local_client.get("/api/tasks", params={"mode": "local"}, headers=auth_headers).json()
    assert listing["total"] >= 1
    assert any(item["id"] == task_id for item in listing["items"]), "本机抓拍未进入历史"

    # 详情：完成、有分类计数、有标注图
    detail = local_client.get(f"/api/tasks/{task_id}", headers=auth_headers).json()
    assert detail["mode"] == "local"
    assert detail["status"] == "done"
    assert detail["progress"] == 100.0
    assert detail["frame_count"] == 1
    assert detail["counts"], "本机抓拍应写入分类计数"
    assert detail["result_image_path"], "本机抓拍应生成标注图"
    # 标注图能匿名取到（与设备上报路径行为一致）
    image = local_client.get(detail["result_image_path"])
    assert image.status_code == 200
    assert len(image.content) > 0


def test_capture_counts_increment_and_timestamp_is_utc(
    local_client: TestClient, auth_headers
):
    first = local_client.post("/api/local-camera/capture", headers=auth_headers).json()
    second = local_client.post("/api/local-camera/capture", headers=auth_headers).json()
    assert first["task_id"] != second["task_id"], "两次抓拍应是两条独立任务"

    status = local_client.get("/api/local-camera/status", headers=auth_headers).json()
    assert status["capture_count"] >= 2
    assert status["last_task_id"] == second["task_id"]
    # 时间戳必须带 Z，否则前端按 UTC 解析会整体偏 8 小时
    assert status["last_capture_at"].endswith("Z"), status["last_capture_at"]


def test_capture_reports_actionable_error_when_source_unusable(
    settings: Settings, tmp_path: Path
):
    """图像源不可用时要报出原因，而不是静默失败或 500 空栈。"""
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    broken = replace(
        settings,
        local_camera=replace(
            settings.local_camera,
            enabled=False,
            backend="simulated",
            simulated_dir=str(empty),
            interval_min=30,
        ),
    )
    app = create_app(broken)
    app.state.detector.load(str(DEFAULT_WEIGHTS))
    with TestClient(app) as client:
        creds = {"username": "broken", "password": "broken123"}
        client.post("/api/auth/register", json=creds)
        headers = {
            "Authorization": "Bearer "
            + client.post("/api/auth/login", json=creds).json()["access_token"]
        }
        response = client.post("/api/local-camera/capture", headers=headers)
        assert response.status_code == 500
        assert "没有图片" in response.json()["detail"]


# ---------------------------------------------------------------- 定时抓拍
def test_start_stop_toggles_scheduler(local_client: TestClient, auth_headers):
    started = local_client.post("/api/local-camera/start", headers=auth_headers).json()
    assert started["running"] is True
    stopped = local_client.post("/api/local-camera/stop", headers=auth_headers).json()
    assert stopped["running"] is False


def test_stop_releases_camera_device(local_client: TestClient, auth_headers):
    """停止后必须释放摄像头设备。

    接口文档承诺"停止并释放"（USB 指示灯熄灭、设备让给别的程序）。
    之前只停了定时线程、设备还开着 —— 实测 camera_open 仍为 true。
    """
    local_client.post("/api/local-camera/capture", headers=auth_headers)
    assert local_client.get("/api/local-camera/status", headers=auth_headers).json()["camera_open"] is True

    stopped = local_client.post("/api/local-camera/stop", headers=auth_headers).json()
    assert stopped["camera_open"] is False, "停止后摄像头未被释放"


def test_zero_interval_never_starts_scheduler(
    local_settings: Settings, tmp_path: Path
):
    """interval_min=0 表示"只手动抓拍"，不该起一个空转的线程。"""
    app = create_app(
        replace(local_settings, local_camera=replace(local_settings.local_camera, interval_min=0))
    )
    app.state.detector.load(str(DEFAULT_WEIGHTS))
    with TestClient(app) as client:
        creds = {"username": "zero", "password": "zero12345"}
        client.post("/api/auth/register", json=creds)
        headers = {
            "Authorization": "Bearer "
            + client.post("/api/auth/login", json=creds).json()["access_token"]
        }
        assert client.post("/api/local-camera/start", headers=headers).json()["running"] is False


def test_scheduler_actually_captures(local_client: TestClient, auth_headers, monkeypatch):
    """真实跑一遍定时抓拍。

    把"一分钟的秒数"改小，让定时器几秒内就触发 —— 否则这个用例要等满一分钟，
    实际结果就是没人跑它，定时这条路等于没有测试覆盖。
    """
    monkeypatch.setattr(local_camera_module, "SECONDS_PER_MINUTE", 0.2)

    assert local_client.post("/api/local-camera/start", headers=auth_headers).json()["running"] is True
    try:
        deadline = time.time() + 15
        count = 0
        while time.time() < deadline:
            status = local_client.get("/api/local-camera/status", headers=auth_headers).json()
            count = status["capture_count"]
            if count >= 1:
                break
            time.sleep(0.2)
        assert count >= 1, "定时器没有产生任何抓拍"
    finally:
        local_client.post("/api/local-camera/stop", headers=auth_headers)

    # 定时抓拍同样要落成历史记录
    listing = local_client.get("/api/tasks", params={"mode": "local"}, headers=auth_headers).json()
    assert listing["total"] >= 1


def test_scheduler_survives_capture_failure(settings: Settings, tmp_path: Path, monkeypatch):
    """单次抓拍失败不能把定时线程弄死 —— 田间接线松动是常态。"""
    empty = tmp_path / "empty2"
    empty.mkdir()
    broken = replace(
        settings,
        local_camera=replace(
            settings.local_camera,
            enabled=False,
            backend="simulated",
            simulated_dir=str(empty),
            # 间隔取 1 分钟：配合被改小的 SECONDS_PER_MINUTE 才是 ~1 秒触发。
            # 若这里写 30，间隔会变成 6 秒，用例就只是在"等"，什么都没验证到。
            interval_min=1,
        ),
    )
    app = create_app(broken)
    app.state.detector.load(str(DEFAULT_WEIGHTS))
    with TestClient(app) as client:
        creds = {"username": "resilient", "password": "resilient123"}
        client.post("/api/auth/register", json=creds)
        headers = {
            "Authorization": "Bearer "
            + client.post("/api/auth/login", json=creds).json()["access_token"]
        }
        monkeypatch.setattr(local_camera_module, "SECONDS_PER_MINUTE", 0.2)
        client.post("/api/local-camera/start", headers=headers)
        # 等到至少发生过一次抓拍尝试（间隔被 max(1, ...) 夹到 1 秒）
        deadline = time.time() + 10
        status = {}
        while time.time() < deadline:
            status = client.get("/api/local-camera/status", headers=headers).json()
            if status.get("last_error"):
                break
            time.sleep(0.3)
        assert status["running"] is True, "定时线程在抓拍失败后不应退出"
        assert status["last_error"], "失败原因应被记录下来"
        client.post("/api/local-camera/stop", headers=headers)
