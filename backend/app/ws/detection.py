# -*- coding: utf-8 -*-
"""WebSocket 实时检测：`/ws/detection`。

**对原桌面端的 WebSocket 服务 的关键修正**：旧实现把 `current_task` / `stop_flag`
放在模块级全局变量里，第二个客户端一连入就会打断第一个的任务。
这里每个连接持有自己的 `DetectionSession`（跟踪器、参数、计数器全隔离），
并用应用内的计数锁限制**同时只允许 1 个流式会话**（本机单实例场景），
既避免 GPU 争抢，也避免多人同时推流把显存打满。

协议（首条必须是 auth）：
  客户端 -> 服务端
    {"type":"auth","token":"<JWT>"}
    {"type":"update_params","conf":0.25,"iou":0.5}
    {"type":"select_model","id":3}
    {"type":"frame","image":"<base64 jpeg>","seq":12}
    {"type":"stop"} / {"type":"ping"}
  服务端 -> 客户端
    {"type":"ready","user":{...},"detector":{...},"params":{...}}
    {"type":"frame_result", detections:[...], seq, frame_total, unique_objects, ...}
    {"type":"task_started"|"task_finished", ...}
    {"type":"pong"} / {"type":"error","code":"...","message":"..."}

刻意**不回传图片**：摄像头场景客户端本来就有原始帧，框在 canvas 上画即可，
相比旧实现的「收图 → 回图」双向 base64 往返，带宽降一个数量级。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..models import Detection as DetectionRow
from ..models import ModelInfo, Task, TaskMode, TaskStatus, User, utcnow
from ..security import decode_access_token
from ..services.alerts import evaluate_task
from ..services.imaging import from_base64_image
from ..services.session import DetectionSession
from ..services.taskmeta import dump_counts

logger = logging.getLogger("insect.ws")
router = APIRouter()

_AUTH_TIMEOUT_S = 15.0
_CLOSE_AUTH_REQUIRED = 4401
_CLOSE_BAD_MESSAGE = 4400

# 摄像头会话的框级明细抽样：每 N 帧记录一次，并设总行数上限。
# 摄像头 10fps 长时间开着，若每帧都写，几分钟就是几万行；
# 聚合计数单独存进 counts_json，因此抽样不会影响汇总数字。
CAMERA_RECORD_EVERY = 5
CAMERA_ROW_CAP = 20_000


def _create_camera_task(
    app: Any, user_id: int, conf: float, iou: float, source_name: str
) -> int:
    """为一次摄像头会话建任务，让它的结果也能进历史 / 看板 / 报告。"""
    db = app.state.session_factory()
    try:
        task = Task(
            mode=TaskMode.CAMERA,
            status=TaskStatus.RUNNING,
            source_name=source_name,
            user_id=user_id,
            conf=conf,
            iou=iou,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task.id
    finally:
        db.close()


def _camera_frame_job(
    app: Any, session: DetectionSession, image: Any, task_id: int, state: dict[str, int]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """在 worker 线程里完成「推理 + 按抽样落库」，返回 (payload, summary)。

    与推理放在同一个线程里，避免每帧多一次线程切换。
    """
    payload, summary = session.process(image)
    seq = int(summary["seq"])

    if seq % CAMERA_RECORD_EVERY == 0 and state.get("rows", 0) < CAMERA_ROW_CAP:
        db = app.state.session_factory()
        try:
            written = state.get("rows", 0)
            for det in payload:
                if written >= CAMERA_ROW_CAP:
                    break
                bbox = det["bbox"]
                db.add(
                    DetectionRow(
                        task_id=task_id,
                        frame_index=seq,
                        class_id=int(det["class_id"]),
                        class_name=str(det["class_name"]),
                        score=float(det["score"]),
                        x1=int(bbox[0]),
                        y1=int(bbox[1]),
                        x2=int(bbox[2]),
                        y2=int(bbox[3]),
                        track_id=det.get("track_id"),
                    )
                )
                written += 1
            db.commit()
            state["rows"] = written
        finally:
            db.close()

    return payload, summary


def _finalize_camera_task(app: Any, task_id: int, session: DetectionSession) -> int:
    """会话结束：把累计统计写回任务表。返回命中的告警条数。"""
    db = app.state.session_factory()
    try:
        task = db.get(Task, task_id)
        if task is None:
            return 0
        task.status = TaskStatus.DONE
        task.frame_count = session.frame_index
        task.total_objects = session.total_objects
        task.unique_objects = session.unique_objects if session.tracking else 0
        task.counts_json = dump_counts(dict(session.class_total))
        task.duration_ms = int((time.time() - session.started_at) * 1000)
        task.finished_at = utcnow()
        db.commit()
        # 与其他模式保持一致：任务完成后评估一次告警规则
        return len(evaluate_task(db, task))
    finally:
        db.close()


def _authenticate(token: str | None, app: Any) -> User | None:
    if not token:
        return None
    settings = app.state.settings
    try:
        # decode_access_token 而不是 decode_token：票据这类带 `purpose` 的令牌
        # 不能当登录态用（它按设计要进 query string，会泄漏）。见 security.py。
        payload = decode_access_token(
            token, settings.resolved_jwt_secret, settings.auth.algorithm
        )
        user_id = int(payload.get("sub", ""))
    except Exception:  # noqa: BLE001 - 令牌问题一律视为未认证
        return None
    db = app.state.session_factory()
    try:
        user = db.get(User, user_id)
        return user if user is not None and user.is_active else None
    finally:
        db.close()


async def _acquire_stream_slot(app: Any) -> bool:
    async with app.state.stream_lock:
        if app.state.active_streams >= app.state.settings.stream.max_sessions:
            return False
        app.state.active_streams += 1
        return True


async def _release_stream_slot(app: Any) -> None:
    async with app.state.stream_lock:
        app.state.active_streams = max(0, app.state.active_streams - 1)


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json({"type": "error", "code": code, "message": message})


@router.websocket("/ws/detection")
async def detection_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    app = websocket.app
    detector = app.state.detector
    settings = app.state.settings

    # ---------------------------------------------------------- 鉴权
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=_AUTH_TIMEOUT_S)
        first = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        await websocket.close(code=_CLOSE_BAD_MESSAGE, reason="需要先发送 auth 消息")
        return

    if not isinstance(first, dict) or first.get("type") != "auth":
        await _send_error(websocket, "auth_required", "首条消息必须是 auth")
        await websocket.close(code=_CLOSE_AUTH_REQUIRED)
        return

    user = await to_thread.run_sync(_authenticate, first.get("token"), app)
    if user is None:
        await _send_error(websocket, "unauthorized", "令牌无效或已过期")
        await websocket.close(code=_CLOSE_AUTH_REQUIRED)
        return

    await websocket.send_json(
        {
            "type": "ready",
            "user": {"id": user.id, "username": user.username},
            "detector": detector.info(),
            "params": detector.params(),
            "stream": {
                "target_fps": settings.stream.target_fps,
                "jpeg_quality": settings.stream.jpeg_quality,
                "max_sessions": settings.stream.max_sessions,
            },
        }
    )

    session: DetectionSession | None = None
    holds_slot = False
    # 摄像头会话对应的任务 id 与明细写入进度（抽样用）
    camera_task_id: int | None = None
    camera_state: dict[str, int] = {"rows": 0}

    async def close_session(reason: str) -> None:
        nonlocal session, holds_slot, camera_task_id
        if session is not None:
            alerts = 0
            if camera_task_id is not None:
                alerts = await to_thread.run_sync(
                    _finalize_camera_task, app, camera_task_id, session
                )
            await websocket.send_json(
                {
                    "type": "task_finished",
                    "reason": reason,
                    "session_id": session.session_id,
                    "task_id": camera_task_id,
                    "frame_count": session.frame_index,
                    "total_objects": session.total_objects,
                    "unique_objects": session.unique_objects if session.tracking else None,
                    "alerts": alerts,
                    "elapsed_s": round(time.time() - session.started_at, 1),
                }
            )
            session = None
            camera_task_id = None
        if holds_slot:
            await _release_stream_slot(app)
            holds_slot = False

    try:
        while True:
            message = json.loads(await websocket.receive_text())
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")

            if message_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if message_type == "update_params":
                conf = message.get("conf")
                iou = message.get("iou")
                if session is not None:
                    session.set_params(conf, iou)
                # 同步到全局默认值，让后续新建的会话沿用
                detector.set_params(conf, iou)
                await websocket.send_json(
                    {
                        "type": "params_updated",
                        "params": {"conf": session.conf if session else detector.conf,
                                   "iou": session.iou if session else detector.iou},
                    }
                )
                continue

            if message_type == "select_model":
                model_id = message.get("id")
                db = app.state.session_factory()
                try:
                    row = db.get(ModelInfo, int(model_id)) if model_id is not None else None
                except (TypeError, ValueError):
                    row = None
                finally:
                    db.close()
                if row is None or not row.exists:
                    await _send_error(websocket, "model_not_found", "模型不存在或文件已丢失")
                    continue
                try:
                    await to_thread.run_sync(detector.load, row.path)
                    await websocket.send_json(
                        {"type": "model_changed", "detector": detector.info()}
                    )
                except Exception as exc:  # noqa: BLE001
                    await _send_error(websocket, "model_load_failed", f"加载失败：{exc}")
                continue

            if message_type == "frame":
                if not detector.loaded:
                    await _send_error(websocket, "model_not_loaded", "模型尚未加载")
                    continue

                image = from_base64_image(message.get("image") or "")
                if image is None:
                    await _send_error(websocket, "bad_frame", "帧数据无法解码")
                    continue

                if session is None:
                    if not await _acquire_stream_slot(app):
                        await _send_error(
                            websocket,
                            "busy",
                            f"已有 {settings.stream.max_sessions} 个流式检测在进行中，请稍后重试",
                        )
                        continue
                    holds_slot = True
                    session = DetectionSession(
                        session_id=uuid.uuid4().hex[:12],
                        mode="camera",
                        detector=detector,
                        conf=detector.conf,
                        iou=detector.iou,
                        tracking=settings.tracking.enabled,
                        iou_thresh=settings.tracking.iou_thresh,
                        max_age=settings.tracking.max_age,
                    )
                    # 建任务：否则摄像头的结果跑完就丢，进不了历史 / 看板 / 报告
                    camera_task_id = await to_thread.run_sync(
                        _create_camera_task,
                        app,
                        user.id,
                        detector.conf,
                        detector.iou,
                        f"摄像头会话 {session.session_id}",
                    )
                    camera_state["rows"] = 0
                    await websocket.send_json(
                        {
                            "type": "task_started",
                            "session_id": session.session_id,
                            "task_id": camera_task_id,
                            "mode": "camera",
                        }
                    )

                detections, summary = await to_thread.run_sync(
                    _camera_frame_job, app, session, image, camera_task_id, camera_state
                )
                await websocket.send_json(
                    {"type": "frame_result", "detections": detections, **summary}
                )
                continue

            if message_type == "stop":
                await close_session("client_stop")
                continue

            if message_type == "reset":
                if session is not None:
                    session.reset()
                    await websocket.send_json({"type": "session_reset"})
                continue

            await _send_error(websocket, "unknown_type", f"未知消息类型：{message_type}")

    except WebSocketDisconnect:
        logger.info("WebSocket 客户端断开")
    except json.JSONDecodeError:
        await _send_error(websocket, "bad_json", "消息不是合法 JSON")
    except Exception as exc:  # noqa: BLE001 - 连接级异常不应影响其它连接
        logger.warning("WebSocket 会话异常：%s", exc)
    finally:
        # 客户端异常断线时不会走 stop，这里把任务收尾，避免留下永远 running 的记录
        if session is not None and camera_task_id is not None:
            try:
                await to_thread.run_sync(_finalize_camera_task, app, camera_task_id, session)
            except Exception as exc:  # noqa: BLE001 - 收尾失败不应再抛
                logger.warning("收尾摄像头任务失败：%s", exc)
        if holds_slot:
            await _release_stream_slot(app)
