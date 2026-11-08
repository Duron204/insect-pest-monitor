# -*- coding: utf-8 -*-
"""设备推流通道：`/ws/agent`。

树莓派主动连上来，把摄像头画面一帧一帧推给中心；中心只做一件事 ——
把**最新一帧**塞进 `FrameHub`，让网页以 MJPEG 取走。中心**不在这里做推理**：
默认帧是原始 JPEG，Pi 的 CPU 全留给定时抓拍；如果用户在盒子本机的设置页
开启了「实时识别」，盒子会先在本机推理、把框画进 JPEG 再推上来 —— 中心收到的
仍然只是一张图，协议不变。

## 与 `/ws/detection` 的关系

两者形状相同（首条必须是 auth、失败即 `close(4401)`），方向相反：

    /ws/detection   浏览器把 base64 帧**推上来**，服务端回框坐标 JSON，不回图
    /ws/agent       盒子把二进制 JPEG **推上来**，服务端什么都不回，只存最新帧

## 两处刻意的偏离

**一、帧走二进制，不走 base64 JSON。**
`ws/detection.py:23-24` 定下的"base64 JSON 帧"约定，前提是那些帧**本来就是**
从浏览器 canvas 出来的 base64，端到端都不必解码。而 Pi 手里是货真价实的 JPEG
字节，再套一层 base64 平白多出 33% 的流量和两次编解码。所以这里 `send_bytes`，
文本帧只留给控制消息（`ping` / `pong` / `status` / `hello` / `error`）。

其中 `status` 是盒子**周期性自报的诊断信息**（推流开关、已推帧数、最近错误……）。
中心自己算不出这些：它只知道"最后一次听到盒子是什么时候"，答不了
"在线但为什么没画面"。存下来给网页的调试面板看，细节见 `presence.update_agent_status`。

**二、令牌走首条消息，不走 query string。**
query 会进访问日志。这与 `/ws/detection` 的做法逐字一致。

## 为什么不做背压

中心不排队：`FrameHub.publish` 是**覆盖**，慢观众自己跳帧（见 frame_hub.py）。
所以这里收到就写、写完就等下一帧，**永远不会因为某个网页看得慢而拖慢盒子**。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..models import Device, DeviceStatus
from ..security import hash_device_token, verify_device_token
from ..services.presence import touch_device, update_agent_status

logger = logging.getLogger("insect.ws")

router = APIRouter()

_AUTH_TIMEOUT_S = 15.0
_CLOSE_AUTH_REQUIRED = 4401
_CLOSE_BAD_MESSAGE = 4400

#: 单帧上限。2fps × 960 宽的 JPEG 通常在 100KB 上下；给到 8MB 是留足余地，
#: 同时拦住"某个坏掉的盒子疯狂推巨帧把内存吃光"。
_MAX_FRAME_BYTES = 8 * 1024 * 1024


def _authenticate_device(token: str | None, app: Any) -> Device | None:
    """按设备令牌取设备。与 `deps.get_current_device` 同一套判断，只是这里没有 Request。"""
    if not token:
        return None
    db = app.state.session_factory()
    try:
        device = (
            db.query(Device).filter(Device.token_hash == hash_device_token(token)).first()
        )
        if device is None or not verify_device_token(token, device.token_hash):
            return None
        if device.status != DeviceStatus.ACTIVE:
            return None
        db.expunge(device)
        return device
    finally:
        db.close()


def _as_bool(value: Any) -> bool | None:
    """只认真正的 bool。

    不把 0/1、"true" 之类折算成布尔：`isinstance(True, int)` 在 Python 里成立，
    一旦放行，`{"streaming": 1}` 就会被后面的 `_as_int` 也接受，两个字段互相串味。
    """
    return value if isinstance(value, bool) else None


def _as_int(value: Any) -> int | None:
    """只认整数（含 3.0 这种**整值**浮点），且**不接受 bool**。

    3.7 这种带小数的直接丢掉，不做 `int()` 截断：帧数差一个不要紧，
    但面板显示的数字必须与盒子实际报的一致，不能由中心悄悄取整。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_text(value: Any, limit: int) -> str | None:
    """截到 limit 的干净文本；空串与空白一律当成"没报"。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _as_datetime(value: Any) -> datetime | None:
    """解析 ISO-8601 时刻。**解析不出来就当没报**，绝不让连接断在这里。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_status(payload: dict[str, Any]) -> dict[str, Any]:
    """把盒子自报的 status 报文**洗**成一组类型确定的字段。

    宽容是刻意的：链路另一头是一台可能跑着旧版本、也可能被人改过的树莓派。
    任何字段类型不对都不该把这条推流连接掐断 —— 画面明明还在推着，
    却因为一条诊断报文格式不对而断线，是最坏的那种失败。
    """
    return {
        "version": _as_text(payload.get("version"), 32),
        "streaming": _as_bool(payload.get("streaming")),
        "stream_enabled": _as_bool(payload.get("stream_enabled")),
        "live_detection": _as_bool(payload.get("live_detection")),
        "frames_sent": _as_int(payload.get("frames_sent")),
        "uploads": _as_int(payload.get("uploads")),
        "pending_uploads": _as_int(payload.get("pending_uploads")),
        "last_error": _as_text(payload.get("last_error"), 500),
        "last_error_at": _as_datetime(payload.get("last_error_at")),
        "camera_backend": _as_text(payload.get("camera_backend"), 32),
    }


def _record_status(app: Any, device_id: int, payload: dict[str, Any]) -> None:
    """盒子自报的诊断信息落库（节流与"翻转即写"在 `update_agent_status` 里）。"""
    clean = _parse_status(payload)
    db = app.state.session_factory()
    try:
        device = db.get(Device, device_id)
        if device is None or device.status != DeviceStatus.ACTIVE:
            return
        update_agent_status(
            db,
            device,
            min_interval_s=app.state.settings.agent.heartbeat_s,
            **clean,
        )
    finally:
        db.close()


def _heartbeat(app: Any, device_id: int, mark_frame: bool) -> None:
    """一次心跳落库（节流在 `presence.touch_device` 里）。

    `mark_frame` **不能写成 keyword-only**：本函数是经 `to_thread.run_sync`
    调用的，而它只透传位置参数，不接受关键字参数。
    """
    db = app.state.session_factory()
    try:
        device = db.get(Device, device_id)
        if device is None or device.status != DeviceStatus.ACTIVE:
            return
        touch_device(
            db,
            device,
            min_interval_s=app.state.settings.agent.heartbeat_s,
            mark_frame=mark_frame,
        )
    finally:
        db.close()


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json({"type": "error", "code": code, "message": message})


@router.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    app = websocket.app
    hub = app.state.frame_hub
    settings = app.state.settings

    # ---------------------------------------------------------- 鉴权
    #
    # 刻意用 `receive()` 自己判消息类型，而不是 `receive_text()`：首帧若是**二进制**
    # （任何人不必持有令牌就能发），Starlette 的 `receive_text()` 会去取
    # `message["text"]` 抛 KeyError，而它不在任何 except 里 —— 结果是 ASGI 打一份
    # 带栈的异常日志，客户端却永远等不到那个约定好的 4401，盒子也就不知道该重新配对。
    try:
        message = await asyncio.wait_for(websocket.receive(), timeout=_AUTH_TIMEOUT_S)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close(code=_CLOSE_BAD_MESSAGE, reason="需要先发送 auth 消息")
        return

    if message.get("type") == "websocket.disconnect":
        return

    raw = message.get("text")
    if not raw:
        await websocket.close(code=_CLOSE_BAD_MESSAGE, reason="首条消息必须是 auth 文本帧")
        return

    try:
        first = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.close(code=_CLOSE_BAD_MESSAGE, reason="需要先发送 auth 消息")
        return

    if not isinstance(first, dict) or first.get("type") != "auth":
        await _send_error(websocket, "auth_required", "首条消息必须是 auth")
        await websocket.close(code=_CLOSE_AUTH_REQUIRED)
        return

    # 令牌必须是字符串：不挡的话 `{"token": 123}` 会一路走到 `hash_device_token`
    # 的 `.encode()` 抛 AttributeError —— 又是一个无需令牌即可触发的未捕获异常。
    token = first.get("token")
    if not isinstance(token, str):
        await _send_error(websocket, "unauthorized", "设备令牌无效或已解绑")
        await websocket.close(code=_CLOSE_AUTH_REQUIRED)
        return

    device = await to_thread.run_sync(_authenticate_device, token, app)
    if device is None:
        # 盒子收到 4401 会清掉本地令牌、回到配对流程 —— 见 edge_agent.py。
        # 刻意不区分"令牌错"与"已被解绑"：两者对盒子的动作完全一样。
        await _send_error(websocket, "unauthorized", "设备令牌无效或已解绑")
        await websocket.close(code=_CLOSE_AUTH_REQUIRED)
        return

    device_id = device.id
    await websocket.send_json(
        {
            "type": "ready",
            "device_id": device_id,
            "name": device.name,
            "stream": {
                "fps": settings.agent.stream_fps,
                "quality": settings.agent.stream_quality,
                "width": settings.agent.stream_width,
            },
        }
    )
    logger.info("设备 %s（%s）推流已接入", device_id, device.name)

    frames = 0
    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            payload = message.get("bytes")
            if payload is not None:
                if not payload:
                    continue
                if len(payload) > _MAX_FRAME_BYTES:
                    logger.warning(
                        "设备 %s 推来一帧 %d 字节，超过上限，已丢弃", device_id, len(payload)
                    )
                    continue
                # 唯一的热路径：覆盖式写入，不排队、不阻塞
                hub.publish(device_id, payload)
                frames += 1
                # 心跳按 heartbeat_s 节流后落库 —— 2fps 每帧都写会把 SD 卡写坏
                await to_thread.run_sync(_heartbeat, app, device_id, True)
                continue

            text = message.get("text")
            if not text:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                await _send_error(websocket, "bad_json", "消息不是合法 JSON")
                continue
            if not isinstance(control, dict):
                continue

            if control.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                # **ping 也要刷新心跳**：不推流的盒子（`stream_enabled=false`）
                # 只有这条 ping 在证明自己还活着，而 `online` 的窗口只有 60 秒。
                # 少了这一下，一个正常工作的盒子会被永久显示成离线。
                await to_thread.run_sync(_heartbeat, app, device_id, False)
                continue
            if control.get("type") == "status":
                # 盒子把自己 `/api/system/agent` 那一屏状态捎上来。
                # 中心只存、只显示，不据此改变任何控制流。
                await to_thread.run_sync(_record_status, app, device_id, control)
                continue
            if control.get("type") == "hello":
                # 不推流的盒子靠它维持在线状态（stream_enabled=false 的配置）
                await to_thread.run_sync(_heartbeat, app, device_id, False)
                continue

            await _send_error(websocket, "unknown_type", f"未知消息类型：{control.get('type')}")

    except WebSocketDisconnect:
        logger.info("设备 %s 推流断开", device_id)
    except Exception as exc:  # noqa: BLE001 - 单条连接出错不该影响其它设备
        logger.warning("设备 %s 推流异常：%s", device_id, exc)
    finally:
        # 断开就把那一帧丢掉。**数据库里的设备行照旧保留** ——
        # 网络断了而已，绑定关系与历史都不该受影响。
        hub.forget(device_id)
        try:
            await to_thread.run_sync(_heartbeat, app, device_id, False)
        except Exception as exc:  # noqa: BLE001 - 收尾失败不该再抛
            logger.warning("设备 %s 收尾心跳失败：%s", device_id, exc)
        logger.info("设备 %s 推流结束，本次共 %d 帧", device_id, frames)
