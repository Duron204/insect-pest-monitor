# -*- coding: utf-8 -*-
"""`FrameHub` 的纯单元测试：没有 socket、没有数据库，全部确定性。

这里锁的核心是一条容易被"优化"掉的性质：**只留最新一帧，绝不排队**。
"""
from __future__ import annotations

import pytest

from app.services import frame_hub as frame_hub_module
from app.services.frame_hub import FrameHub


class _FakeClock:
    """可控的单调时钟，让帧率断言不依赖真实时间。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(frame_hub_module, "time", fake)
    return fake


# ---------------------------------------------------------------- 只留一帧
def test_publish_overwrites_and_never_queues():
    """⭐ 发 100 帧之后，只能读到最后一帧，且只缓存了 1 帧。

    这条是整个实时画面的地基：如果哪天有人把 `publish` 改成往队列里塞
    （"别丢帧嘛"），观众就会越看越落后 —— 落后的直播比丢帧糟糕得多。
    `frame_count(...) == 1` 是可证伪的那一条。
    """
    hub = FrameHub()
    for i in range(100):
        hub.publish(7, b"frame-%d" % i)

    frame = hub.latest(7)
    assert frame is not None
    assert frame.jpeg == b"frame-99"
    assert frame.seq == 99
    assert hub.frame_count(7) == 1
    assert hub.size() == 1


def test_seq_increases_monotonically():
    hub = FrameHub()
    seqs = [hub.publish(1, b"x") for _ in range(5)]
    assert seqs == [0, 1, 2, 3, 4]
    assert hub.seq_of(1) == 4


def test_latest_and_seq_of_are_empty_for_unknown_device():
    hub = FrameHub()
    assert hub.latest(42) is None
    assert hub.seq_of(42) == -1
    assert hub.frame_count(42) == 0


# ---------------------------------------------------------------- 内存上界
def test_unknown_device_is_dropped_when_full():
    """满了之后**新**设备被丢弃、字典不增长 —— 这是内存上界唯一的硬约束。"""
    hub = FrameHub(max_devices=2)
    assert hub.publish(1, b"a") == 0
    assert hub.publish(2, b"b") == 0
    assert hub.publish(3, b"c") == -1
    assert hub.size() == 2
    assert hub.latest(3) is None


def test_known_device_keeps_publishing_when_full():
    """满了不该把已在册的设备也卡住 —— 否则第 33 台一连上，前 32 台就全哑了。"""
    hub = FrameHub(max_devices=2)
    hub.publish(1, b"a")
    hub.publish(2, b"b")
    assert hub.publish(1, b"a2") == 1
    assert hub.latest(1).jpeg == b"a2"


# ---------------------------------------------------------------- 清理
def test_forget_removes_only_that_device():
    hub = FrameHub()
    hub.publish(1, b"a")
    hub.publish(2, b"b")
    hub.forget(1)
    assert hub.latest(1) is None
    assert hub.latest(2) is not None
    hub.forget(1)  # 幂等：重复忘掉不该炸
    assert hub.size() == 1


def test_evict_stale_drops_old_frames(clock):
    hub = FrameHub()
    hub.publish(1, b"old")
    clock.now += 31.0
    hub.publish(2, b"fresh")

    assert hub.evict_stale(30.0) == [1]
    assert hub.latest(1) is None
    assert hub.latest(2) is not None


# ---------------------------------------------------------------- 帧率
def test_fps_is_zero_when_it_cannot_be_measured(clock):
    """样本不足、或跨度算不出来（0）时，都返回 0.0 —— **不编一个数**。

    与 `HostStatusOut` 的「`None` 表示没测到」同一条约定。
    这里不能返回一个看起来合理的缺省值（比如配置里的目标帧率）：
    那会让"没在推流"和"推得很稳"在界面上长得一模一样。
    """
    hub = FrameHub()
    assert hub.fps_of(1) == 0.0, "一帧都没有"
    hub.publish(1, b"a")
    assert hub.fps_of(1) == 0.0, "只有一个样本，算不出间隔"
    hub.publish(1, b"b")
    assert hub.fps_of(1) == 0.0, "两帧落在同一时刻，跨度为 0，不能拿它当除数"


def test_fps_measures_a_real_interval(clock):
    hub = FrameHub()
    for _ in range(3):
        hub.publish(1, b"x")
        clock.now += 1.0
    # 3 个样本、间隔各 1 秒，跨度 2 秒 → 2 / 2 = 1.0 fps
    assert hub.fps_of(1) == pytest.approx(1.0, abs=0.01)


def test_fps_measures_the_recent_window(clock):
    hub = FrameHub(fps_window=12)
    for _ in range(12):
        hub.publish(1, b"x")
        clock.now += 0.5  # 2 fps
    # 12 个样本、11 个间隔，跨度 5.5 秒 → 11 / 5.5 = 2.0
    assert hub.fps_of(1) == pytest.approx(2.0, abs=0.01)


def test_fps_window_slides(clock):
    """窗口只回看最近 N 个样本 —— 否则一台跑了三小时的盒子，
    帧率会被三个小时前的样本稀释成一个永远平滑的假数字。"""
    hub = FrameHub(fps_window=4)
    for _ in range(20):
        hub.publish(1, b"x")
        clock.now += 1.0  # 1 fps
    assert hub.fps_of(1) == pytest.approx(1.0, abs=0.01)
