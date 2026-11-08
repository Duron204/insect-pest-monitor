# -*- coding: utf-8 -*-
"""每台设备只保留**最新一帧**的内存中转站。

**为什么只留一帧，不留队列**：这是"看现场直播"，不是"录像"。
留队列就会有积压，而积压意味着观众看到的是几十秒前的画面 ——
那比丢帧糟糕得多（丢帧至少画面还是"现在"的）。
所以策略是 **永远覆盖，绝不排队**。

于是丢帧策略本身就消失了：慢观众每次只读 `latest()` 并与上次的 `seq` 比对，
只看新帧，从不重放历史。生产者永远不会被消费者拖慢。

**为什么是内存而不是数据库/磁盘**：帧率 2fps、每帧上百 KB，
落盘既会写爆 SD 卡，也毫无意义 —— 这些帧的寿命以秒计，进程重启后本就该没有。
设备本身的"存在"由数据库里的 `devices` 行表达，这里只管"画面上现在是什么"。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import NamedTuple

__all__ = ["FrameHub", "Frame"]


class Frame(NamedTuple):
    """某一台设备最近一帧的样子。"""

    seq: int        # 单调递增，观众靠它判断"是不是新帧"
    ts: float       # 单调时钟（time.monotonic），用于判过期与算帧率
    jpeg: bytes


class _Entry(NamedTuple):
    frame: Frame
    #: 最近若干次的发布时间，只用来算实测帧率 —— 不存帧本身
    stamps: deque[float]


class FrameHub:
    """设备 -> 最新一帧的线程安全字典。

    写入来自 WebSocket 的接收循环（asyncio 线程），读取来自 MJPEG 的异步生成器，
    两者可能在不同线程上，所以用一把普通的 `threading.Lock`。持锁时间是一次字典
    赋值（微秒级），不构成竞争热点。
    """

    def __init__(self, max_devices: int = 32, fps_window: int = 12) -> None:
        self._entries: dict[int, _Entry] = {}
        self._lock = threading.Lock()
        self._max_devices = max_devices
        #: 算实测帧率时回看的样本数。12 个样本在 2fps 下约合 6 秒，
        #: 足够平滑又不至于把"刚断流"这件事掩盖掉。
        self._fps_window = fps_window

    # ---------------------------------------------------------------- 写
    def publish(self, device_id: int, jpeg: bytes) -> int:
        """覆盖式写入一帧，返回它的 seq。

        超出 `max_devices` 且是**未知设备**时直接丢弃并返回 -1：
        宁可少一路画面，也不能让这个字典无界增长 —— 内存上界是这里唯一的硬约束。
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(device_id)
            if entry is None:
                if len(self._entries) >= self._max_devices:
                    return -1
                stamps: deque[float] = deque(maxlen=self._fps_window)
                seq = 0
            else:
                stamps = entry.stamps
                seq = entry.frame.seq + 1
            stamps.append(now)
            self._entries[device_id] = _Entry(Frame(seq=seq, ts=now, jpeg=jpeg), stamps)
            return seq

    # ---------------------------------------------------------------- 读
    def latest(self, device_id: int) -> Frame | None:
        with self._lock:
            entry = self._entries.get(device_id)
            return entry.frame if entry is not None else None

    def seq_of(self, device_id: int) -> int:
        """最近一帧的 seq；没有则 -1。"""
        with self._lock:
            entry = self._entries.get(device_id)
            return entry.frame.seq if entry is not None else -1

    def fps_of(self, device_id: int) -> float:
        """最近一个窗口的实测帧率。样本不足两个时返回 0.0（而不是编一个数）。"""
        with self._lock:
            entry = self._entries.get(device_id)
            if entry is None or len(entry.stamps) < 2:
                return 0.0
            stamps = list(entry.stamps)
        span = stamps[-1] - stamps[0]
        if span <= 0:
            return 0.0
        return round((len(stamps) - 1) / span, 2)

    def size(self) -> int:
        """当前缓存了几台设备。测试用它断言"只留一帧、绝不积压"。"""
        with self._lock:
            return len(self._entries)

    def frame_count(self, device_id: int) -> int:
        """该设备缓存了几帧 —— 永远是 0 或 1。"""
        with self._lock:
            return 1 if device_id in self._entries else 0

    # ---------------------------------------------------------------- 清理
    def forget(self, device_id: int) -> None:
        with self._lock:
            self._entries.pop(device_id, None)

    def evict_stale(self, max_age_s: float = 30.0) -> list[int]:
        """清掉太久没更新的设备帧，返回被清掉的设备 id。

        只丢内存里那一帧，**数据库的 devices 行照旧保留** ——
        设备可能只是网络断了，历史记录和绑定关系都不该受影响。
        """
        cutoff = time.monotonic() - max_age_s
        with self._lock:
            dead = [did for did, entry in self._entries.items() if entry.frame.ts < cutoff]
            for did in dead:
                del self._entries[did]
        return dead
