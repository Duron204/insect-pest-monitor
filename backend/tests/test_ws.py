# -*- coding: utf-8 -*-
"""WebSocket 实时检测测试。

重点验证原桌面端的 WebSocket 服务 的两个毛病没有重现：
1. **每连接状态隔离** —— 旧实现用模块级 `current_task` / `stop_flag`，
   第二个客户端一连入就打断第一个的任务；
2. **参数更新下一帧生效** —— 旧实现的 `update_params` 是空的 `pass`；
3. **流式会话配额** —— 单实例下同时只允许 1 路流，第二路应收到 busy。
"""
from __future__ import annotations

import base64
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.ws.detection import CAMERA_RECORD_EVERY

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE = PROJECT_ROOT / "test_media" / "InsectPest_SIXU_A00025.jpg"
DEFAULT_WEIGHTS = PROJECT_ROOT / "weights" / "best-yolo11s.pt"
#: 权重被 .gitignore 排除（18MB 二进制不进仓库），因此 CI 上没有它。
#: 这种情况**显式跳过**并在报告里写清原因，而不是让 CI 无故失败。
pytestmark = pytest.mark.skipif(
    not DEFAULT_WEIGHTS.is_file(), reason=f"缺少权重 {DEFAULT_WEIGHTS.name}（仓库不存二进制）"
)

@pytest.fixture()
def loaded_app(app):
    app.state.detector.load(str(DEFAULT_WEIGHTS))
    return app


@pytest.fixture()
def token(client) -> str:
    client.post(
        "/api/auth/register", json={"username": "wstester", "password": "secret123"}
    )
    return client.post(
        "/api/auth/login", json={"username": "wstester", "password": "secret123"}
    ).json()["access_token"]


def _frame_b64(max_side: int = 640) -> str:
    """把真实测试图编码成 base64 JPEG（模拟前端抽帧上传）。中文路径安全读图。"""
    buffer = np.fromfile(str(TEST_IMAGE), dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    assert image is not None
    height, width = image.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale < 1.0:
        image = cv2.resize(image, (int(width * scale), int(height * scale)))
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _auth(ws, token: str) -> dict:
    ws.send_json({"type": "auth", "token": token})
    ready = ws.receive_json()
    assert ready["type"] == "ready"
    return ready


def _drain_until(ws, wanted: str, limit: int = 8) -> dict:
    """读到指定类型的消息为止。

    task_started 只在会话建立时发一次，且它可能位于 frame_result 之前，
    所以不能按固定次数 recv（否则会错位甚至永久阻塞）。
    """
    for _ in range(limit):
        message = ws.receive_json()
        if message.get("type") == wanted:
            return message
    raise AssertionError(f"{limit} 条消息内未收到 {wanted}")


# ---------------------------------------------------------------- 摄像头会话落库
def test_camera_session_creates_and_finalizes_a_task(loaded_app, client, token):
    """摄像头结果必须落库，否则跑完就丢：进不了历史 / 看板 / 报告。"""
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        ws.send_json({"type": "frame", "image": _frame_b64()})

        started = _drain_until(ws, "task_started")
        assert started["mode"] == "camera"
        task_id = started.get("task_id")
        assert task_id, "task_started 必须带上 task_id，前端才能跳转到历史明细"

        assert _drain_until(ws, "frame_result")["frame_total"] == 1

        ws.send_json({"type": "stop"})
        finished = _drain_until(ws, "task_finished")
        assert finished["task_id"] == task_id

    headers = {"Authorization": f"Bearer {token}"}
    detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
    assert detail["mode"] == "camera"
    assert detail["status"] == "done"
    assert detail["progress"] == 100.0
    assert detail["frame_count"] == 1
    assert detail["total_objects"] >= 1
    assert detail["unique_objects"] >= 1
    assert detail["counts"], "应写入分类计数"
    assert detail["started_at"].endswith("Z")
    assert detail["finished_at"].endswith("Z")


def test_camera_session_shows_up_in_task_history(loaded_app, client, token):
    """会话结束后应能在历史列表里按 camera 过滤到。"""
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        ws.send_json({"type": "frame", "image": _frame_b64()})
        _drain_until(ws, "frame_result")
        ws.send_json({"type": "stop"})
        _drain_until(ws, "task_finished")

    headers = {"Authorization": f"Bearer {token}"}
    listing = client.get("/api/tasks", params={"mode": "camera"}, headers=headers).json()
    assert listing["total"] >= 1
    assert listing["items"][0]["mode"] == "camera"


def test_camera_detection_rows_are_sampled_not_every_frame(loaded_app, client, token):
    """长会话不能每帧都写明细：每 CAMERA_RECORD_EVERY 帧记录一次。

    seq 从 1 开始，6 帧只命中 seq=5，因此明细行数应明显少于累计框数 ——
    若这里相等，说明抽样失效、又在逐帧写了。
    """
    frames = CAMERA_RECORD_EVERY + 1
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        for _ in range(frames):
            ws.send_json({"type": "frame", "image": _frame_b64()})
            _drain_until(ws, "frame_result")
        ws.send_json({"type": "stop"})
        task_id = _drain_until(ws, "task_finished")["task_id"]

    headers = {"Authorization": f"Bearer {token}"}
    detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
    assert detail["frame_count"] == frames
    assert detail["total_objects"] == frames        # 每帧检出 1 个

    counts = client.get(f"/api/tasks/{task_id}/counts", headers=headers).json()
    assert counts["detection_rows"] > 0
    assert counts["detection_rows"] < detail["total_objects"], (
        f"明细行数 {counts['detection_rows']} 不应等于累计框数 "
        f"{detail['total_objects']}，否则说明没有抽样"
    )


def test_camera_session_records_class_counts(loaded_app, client, token):
    """分类计数（框数口径）要写进 counts_json，看板才能按虫种统计。"""
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        ws.send_json({"type": "frame", "image": _frame_b64()})
        _drain_until(ws, "frame_result")
        ws.send_json({"type": "stop"})
        task_id = _drain_until(ws, "task_finished")["task_id"]

    headers = {"Authorization": f"Bearer {token}"}
    counts = client.get(f"/api/tasks/{task_id}/counts", headers=headers).json()
    assert counts["counts"], "counts 不应为空"
    assert sum(counts["counts"].values()) == counts["total_objects"]


# ---------------------------------------------------------------- 鉴权
def test_first_message_must_be_auth(loaded_app, client):
    with client.websocket_connect("/ws/detection") as ws:
        ws.send_json({"type": "ping"})
        data = ws.receive_json()
        assert data["type"] == "error"
        assert data["code"] == "auth_required"


def test_bad_token_is_rejected(loaded_app, client):
    with client.websocket_connect("/ws/detection") as ws:
        ws.send_json({"type": "auth", "token": "not-a-jwt"})
        data = ws.receive_json()
        assert data["type"] == "error"
        assert data["code"] == "unauthorized"


def test_ready_reports_detector_state(loaded_app, client, token):
    with client.websocket_connect("/ws/detection") as ws:
        ready = _auth(ws, token)
        assert ready["detector"]["loaded"] is True
        assert ready["detector"]["num_classes"] == 15
        assert "params" in ready


# ---------------------------------------------------------------- 帧检测
def test_frame_returns_detections_without_image(loaded_app, client, token):
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        ws.send_json({"type": "frame", "image": _frame_b64()})

        started = ws.receive_json()
        assert started["type"] == "task_started"

        result = ws.receive_json()
        assert result["type"] == "frame_result"
        # 刻意不回传图片：摄像头场景客户端本地已有该帧
        assert "image" not in result
        assert result["frame_total"] >= 1
        assert result["unique_objects"] == result["frame_total"]
        assert result["inference_ms"] > 0
        assert result["detections"][0]["zh_name"], "中文类别名未补全"

        ws.send_json({"type": "stop"})
        finished = ws.receive_json()
        assert finished["type"] == "task_finished"
        assert finished["frame_count"] == 1


def test_same_frame_repeated_counts_one_unique_object(loaded_app, client, token):
    """去重计数的核心行为：同一只虫在多帧里只算一个。"""
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        frame = _frame_b64()
        last = None
        for _ in range(3):
            ws.send_json({"type": "frame", "image": frame})
            if last is None:
                assert ws.receive_json()["type"] == "task_started"
            last = ws.receive_json()
            assert last["type"] == "frame_result"

        assert last["seq"] == 3
        # 逐帧框数会累加，但去重虫数应等于单帧的框数
        assert last["total_objects"] == last["frame_total"] * 3
        assert last["unique_objects"] == last["frame_total"]


# ---------------------------------------------------------------- 参数
def test_update_params_takes_effect_next_frame(loaded_app, client, token):
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        ws.send_json({"type": "update_params", "conf": 0.95, "iou": 0.5})
        updated = ws.receive_json()
        assert updated["type"] == "params_updated"
        assert updated["params"]["conf"] == pytest.approx(0.95)

        ws.send_json({"type": "frame", "image": _frame_b64()})
        assert ws.receive_json()["type"] == "task_started"
        result = ws.receive_json()
        for det in result["detections"]:
            assert det["score"] >= 0.95, "高阈值未生效"


def test_unknown_message_type_reports_error(loaded_app, client, token):
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        ws.send_json({"type": "definitely-not-a-real-type"})
        data = ws.receive_json()
        assert data["type"] == "error"
        assert data["code"] == "unknown_type"


def test_ping_pong(loaded_app, client, token):
    with client.websocket_connect("/ws/detection") as ws:
        _auth(ws, token)
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


# ---------------------------------------------------------------- 配额
def test_second_stream_is_rejected_as_busy(loaded_app, client, token):
    """旧实现下第二个连接会直接打断第一个任务；现在应被明确拒绝。"""
    with client.websocket_connect("/ws/detection") as first:
        _auth(first, token)
        first.send_json({"type": "frame", "image": _frame_b64()})
        assert first.receive_json()["type"] == "task_started"
        assert first.receive_json()["type"] == "frame_result"

        with client.websocket_connect("/ws/detection") as second:
            _auth(second, token)
            second.send_json({"type": "frame", "image": _frame_b64()})
            data = second.receive_json()
            assert data["type"] == "error"
            assert data["code"] == "busy"

        # 第一个连接必须仍然可用（未被第二路干扰）
        first.send_json({"type": "frame", "image": _frame_b64()})
        result = first.receive_json()
        assert result["type"] == "frame_result"
        assert result["seq"] == 2


def test_slot_released_after_stop(loaded_app, client, token):
    with client.websocket_connect("/ws/detection") as first:
        _auth(first, token)
        first.send_json({"type": "frame", "image": _frame_b64()})
        first.receive_json()
        first.receive_json()
        first.send_json({"type": "stop"})
        assert first.receive_json()["type"] == "task_finished"

        # 释放后第二路应当能拿到配额
        with client.websocket_connect("/ws/detection") as second:
            _auth(second, token)
            second.send_json({"type": "frame", "image": _frame_b64()})
            assert second.receive_json()["type"] == "task_started"
