# -*- coding: utf-8 -*-
"""设备在线状态的判定与心跳节流。

单独成模块是因为它有两个调用方（`routers/devices.py` 的查询侧、
`routers/devices.py` 与 `ws/agent.py` 的写入侧），而这两侧必须用**同一个口径** ——
否则会出现"设备自己觉得在线、网页显示离线"这种没法查的分歧。

## 两条规则

**一、在线永远是算出来的，绝不落库。**
库里只存 `last_seen_at`（事实：最后一次听到它的时刻），
`online` 是查询时由它推出来的结论。落库的话，进程被 kill 或者主机断电之后，
那个字段会永远停在 `true` —— 一个只有重启数据库才能纠正的谎。

**二、心跳写库要节流。**
盒子按 2fps 推流，如果每帧都写一次 `last_seen_at`，就是每秒两次写 SD 卡。
树莓派的 SD 卡是被写坏的，不是被用坏的。所以默认 10 秒最多落一次库。

## 一个刻意的取舍：`online` 只看心跳，不看有没有画面

计划里原本写的是 `online = 有心跳 且 内存里有它的帧`。实现时去掉了后半条，
因为那会让一个**完全合法**的配置永远显示离线：`agent.stream_enabled = false`
（只上报结果、不推视频，Pi 的 CPU 全留给推理）时永远没有帧，
可这台盒子明明在正常工作。

"有没有画面"已经由 `stream_fps`（0 就是没在推）和前端那句"正在连接…"表达清楚了，
不需要塞进 `online` 里让它的含义变模糊 —— `online` 就该只回答一件事：**这台盒子还在跟我说话吗**。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Device, utcnow

__all__ = [
    "ONLINE_WINDOW_S",
    "DEFAULT_HEARTBEAT_S",
    "is_online",
    "touch_device",
    "update_agent_status",
]

#: 最近这么久之内还听到过它，就算在线。
#: 取 60 秒是为了容得下一次 WS 重连（退避上限 60 秒）而不至于误报离线。
ONLINE_WINDOW_S = 60.0

#: 心跳写库的最小间隔（秒），与 `AgentConfig.heartbeat_s` 的默认值一致。
DEFAULT_HEARTBEAT_S = 10.0


def is_online(device: Device, now: datetime | None = None, *, window_s: float = ONLINE_WINDOW_S) -> bool:
    """这台设备现在算不算在线。**纯函数**，不碰数据库也不碰内存 —— 冻结时钟即可测。"""
    if device.last_seen_at is None:
        return False
    return ((now or utcnow()) - device.last_seen_at).total_seconds() <= window_s


def touch_device(
    db: Session,
    device: Device,
    *,
    now: datetime | None = None,
    min_interval_s: float = DEFAULT_HEARTBEAT_S,
    mark_frame: bool = False,
) -> bool:
    """记一次"听到设备了"，返回**这次是否真的写了库**。

    节流是必须的：见模块 docstring 的第二条。返回值让调用方能在测试里
    直接断言"连打 5 次心跳只写了一次"，而不是去猜。

    `mark_frame=True` 时顺带把 `last_frame_at` 也推到此刻。做成同一个参数
    而不是两次调用，是为了**一次心跳只写一次库** —— 推流时每帧都调它，
    分成两次写就会有两倍的 SD 卡写入。
    """
    now = now or utcnow()
    last = device.last_seen_at
    if last is not None and (now - last).total_seconds() < min_interval_s:
        return False
    device.last_seen_at = now
    if mark_frame:
        device.last_frame_at = now
    db.commit()
    return True


def update_agent_status(
    db: Session,
    device: Device,
    *,
    now: datetime | None = None,
    min_interval_s: float = DEFAULT_HEARTBEAT_S,
    version: str | None = None,
    streaming: bool | None = None,
    stream_enabled: bool | None = None,
    live_detection: bool | None = None,
    frames_sent: int | None = None,
    uploads: int | None = None,
    pending_uploads: int | None = None,
    last_error: str | None = None,
    last_error_at: datetime | None = None,
    camera_backend: str | None = None,
) -> bool:
    """记一次盒子自报的诊断信息，返回**这次是否真的写了库**。

    status 报文本身就是"盒子还在跟我说话"的证据，所以它**顺带顶一次心跳**
    （`last_seen_at`）；这样不推流的盒子光靠 status 也能维持在线，不必再
    额外发一条 heartbeat。

    ## 为什么不是每条都写库

    节流与 `touch_device` 同理（SD 卡是写坏的，不是用坏的）。但有一条例外：
    **状态发生了变化就必须立刻写**。刚报上来的"摄像头打不开"要是被节流挡掉，
    面板就会在真正出问题的时间窗里显示"一切正常" —— 那这个面板就白做了。
    所以只要开关翻转、版本号变了、或者出现了新的错误，就无视节流直接落库。

    ## 为什么 `None` 表示"这一项没报"，而不是"这一项是空"

    盒子可能跑的是旧版本、不认识某个字段。此时用 None 覆盖掉既有值是**丢信息**：
    旧盒子报上来的 128 帧，不该因为新版本少发一个字段就被抹成 NULL。
    所以每个字段只在**非 None** 时才写入。
    """
    now = now or utcnow()

    heartbeat_due = (
        device.last_seen_at is None
        or (now - device.last_seen_at).total_seconds() >= min_interval_s
    )
    # 会"翻转"的那些字段：变了就要马上落库，不等节流窗口
    flips = (
        (version is not None and version != device.agent_version),
        (streaming is not None and streaming != device.agent_streaming),
        (stream_enabled is not None and stream_enabled != device.agent_stream_enabled),
        (live_detection is not None and live_detection != device.agent_live_detection),
        (last_error is not None and last_error != device.agent_last_error),
        (camera_backend is not None and camera_backend != device.agent_camera_backend),
    )
    if not heartbeat_due and not any(flips):
        return False

    if heartbeat_due:
        device.last_seen_at = now
    if version is not None:
        # 每次都跟着 status 校一遍：盒子是"拷文件夹"升级的，这一步让
        # 中心显示的版本号**自己**跟上，不依赖重新配对。
        device.agent_version = version
    if streaming is not None:
        device.agent_streaming = streaming
    if stream_enabled is not None:
        device.agent_stream_enabled = stream_enabled
    if live_detection is not None:
        device.agent_live_detection = live_detection
    if frames_sent is not None:
        device.agent_frames_sent = frames_sent
    if uploads is not None:
        device.agent_uploads = uploads
    if pending_uploads is not None:
        device.agent_pending_uploads = pending_uploads
    if last_error is not None:
        # 时刻只在**错误内容变了**的时候刷新。否则同一条错误会被后续每一次
        # status 报文顶到"刚刚"，看面板的人会以为它正在发生。
        if last_error != device.agent_last_error or device.agent_last_error_at is None:
            # 时刻优先用盒子给的（它记录的是真正出错那一刻），
            # 旧盒子不给才退回"中心收到的时刻"。
            device.agent_last_error_at = last_error_at or now
        device.agent_last_error = last_error
    if camera_backend is not None:
        # 画面来源变了（比如从 simulated 翻到 picamera2）要立刻落库：这恰恰
        # 是排查"为什么画面是训练照片"时最需要被看见的那一跳，不能等节流。
        device.agent_camera_backend = camera_backend
    device.agent_status_at = now
    db.commit()
    return True
