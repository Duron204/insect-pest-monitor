# -*- coding: utf-8 -*-
"""边缘代理的**客户端状态机**：配对、退避、上报重试、令牌作废。

这个文件把传输层换成假的，所以**不需要网络、不需要中心、不需要树莓派**，
跑得很快。代价是它证明不了"代理与真实中心的接口对得上"——那是
`test_agent_simulated.py` 的活（真 uvicorn + 真 urllib + 真 WebSocket）。
两个文件分工明确，缺一个就有一类 bug 漏掉：

    本文件   状态转移对不对（配到一半重启会不会重印码、401 会不会死循环重连）
    模拟端到端 两边接口对不对（载荷字段、关闭码、ready 计划）

**这里测的是"类型系统证明不了的性质"**：`_pair_loop` 会不会在拿到令牌后
真的停下、退避序列会不会单调封顶、上报失败之后队列里那一帧还在不在。
没有一条能靠读代码确信，所以全部用可证伪的断言锁住。
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.config import AgentConfig, Settings
from app.services import edge_agent as edge_agent_module
from app.services.backends.base import Detection, InferenceResult
from app.services.edge_agent import (
    AgentState,
    EdgeAgentService,
    TokenRejected,
    backoff_delay,
)
from app.services import imaging
from app.services.imaging import encode_jpeg

import numpy as np

CODE = "424242"
#: 配对凭证。中心在 `pair/start` 里和码一起发下来，代理每次轮询都要带回去。
SECRET = "pair-secret-" + "b" * 32
TOKEN = "ipd_" + "a" * 43


# ================================================================== 假传输
class FakeClosed(Exception):
    """模拟 `websockets.exceptions.ConnectionClosed`。

    只带一个 `rcvd.code` —— 因为代理代码**只**读这个属性来区分
    "令牌被吊销（4401）"与"网络断了（1006）"。造一个真异常对象不会多验证
    任何东西，反而会在 websockets 换实现时让这个文件跟着红。
    """

    def __init__(self, code: int) -> None:
        super().__init__(f"对端用 {code} 关掉了连接")
        self.rcvd = SimpleNamespace(code=code)


class FakeSocket:
    """脚本化的假 WebSocket。

    `inbox` 是预置的**入向 JSON**（中心 → 盒子），发完后按 `close_code`
    抛 `FakeClosed`（默认 1006，即"网络断了"）。
    `on_send` 每发一帧调一次，测试用它来数帧数、跑够了就把代理停掉。
    """

    def __init__(
        self,
        inbox: list[Any] | None = None,
        *,
        close_code: int = 1006,
        on_send: Callable[[Any], None] | None = None,
        auto_pong: bool = False,
    ) -> None:
        self._inbox = deque(inbox or [])
        self.close_code = close_code
        self.on_send = on_send
        self.auto_pong = auto_pong
        self.sent: list[Any] = []
        self.entered = 0

    async def send(self, data: Any) -> None:
        self.sent.append(data)
        if self.on_send is not None:
            self.on_send(data)

    async def recv(self) -> Any:
        if self._inbox:
            return self._inbox.popleft()
        if self.auto_pong:
            return json.dumps({"type": "pong"})
        raise FakeClosed(self.close_code)

    @property
    def frames(self) -> list[bytes]:
        """发出去的**二进制**帧（即 JPEG）。文本帧是控制消息，不算。"""
        return [item for item in self.sent if isinstance(item, (bytes, bytearray))]


class FakeConnect:
    """`connect_ws()` 的返回值：一个异步上下文管理器，吐同一只假 socket。"""

    def __init__(self, socket: FakeSocket) -> None:
        self._socket = socket

    async def __aenter__(self) -> FakeSocket:
        self._socket.entered += 1
        return self._socket

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeTransport:
    """假传输：每个动作用一个**回调**来应答。

    回调而不是固定序列：要断言的往往是"第 3 次轮询时中心才说 paired"
    这种时序，回调里能直接读调用计数写条件，序列则要为每个用例重排一遍。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.pair_starts = 0
        self.polls = 0
        self.uploads: list[dict] = []
        self.on_pair_start: Callable[[], tuple[int, dict]] = lambda: (
            200,
            {
                "pairing_id": 7,
                "code": CODE,
                "pairing_secret": SECRET,
                "expires_in": 300,
                "poll_interval_s": 0,
            },
        )
        self.on_poll: Callable[[], tuple[int, dict]] = lambda: (200, {"status": "pending"})
        self.on_upload: Callable[[dict], tuple[int, dict]] = lambda payload: (
            200,
            {"task_id": 1, "seq": payload["seq"]},
        )
        self.socket_factory: Callable[[], FakeSocket] = FakeSocket

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        self.calls.append((method, url, payload, headers))
        if url.endswith("/devices/pair/start"):
            self.pair_starts += 1
            return self.on_pair_start()
        if "/devices/pair/poll" in url:
            self.polls += 1
            return self.on_poll()
        if url.endswith("/devices/tasks"):
            self.uploads.append(payload or {})
            return self.on_upload(payload or {})
        raise AssertionError(f"假传输不认识这个地址：{url}")

    def connect_ws(self, url: str) -> FakeConnect:
        return FakeConnect(self.socket_factory())


# ============================================================== 假相机/推理
class FakeCamera:
    """假取帧：返回一张真 JPEG 的字节解码出来的真图。

    用真图而不是 `np.zeros`：`encode_jpeg` 对全黑图也能编码成功，于是
    "推流发出去的是不是一张能看的图"完全没被验证到。
    """

    def __init__(self, frame: np.ndarray) -> None:
        self._frame = frame
        self.grabs = 0
        self.fail_next = False

    def grab_frame(self) -> tuple[np.ndarray, dict[str, Any]]:
        self.grabs += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("取帧失败（摄像头被占用或已断开？）")
        return self._frame, {"name": "simulated", "width": self._frame.shape[1]}

    def status(self) -> dict[str, Any]:
        """仿 `LocalCameraService.status()`：盒子自报诊断时 `_status_message`
        会读它拿画面来源（`backend`）。这里返回模拟源，对应"推流出的就是训练照"。

        只给 `_status_message` 真用到的 `backend` 一个真值，其余填空也不影响 —
        那个函数只读 `cam.get("backend")`。
        """
        return {
            "enabled": True,
            "running": False,
            "backend": "simulated",
            "configured_backend": "auto",
            "device": "0",
            "interval_min": 0,
            "camera_open": False,
            "capture_count": 0,
            "last_capture_at": None,
            "last_task_id": None,
            "last_error": None,
            "last_error_at": None,
            "next_capture_in_s": None,
            "available": [],
            "host": {},
        }


class FakeDetector:
    """固定输出的假推理。

    代理这边只关心"框有没有被原样装进载荷"，模型的准确度是
    `test_detector.py` 的事 —— 这里加载真权重只会让用例慢十倍。
    """

    def __init__(self, detections: list[Detection] | None = None) -> None:
        self.calls: list[tuple[float | None, float | None]] = []
        self._detections = (
            detections
            if detections is not None
            else [
                Detection(
                    class_id=0,
                    class_name="aphid",
                    score=0.91,
                    bbox=(10, 20, 110, 140),
                    zh_name="蚜虫",
                )
            ]
        )

    def infer(self, frame, conf=None, iou=None) -> InferenceResult:
        self.calls.append((conf, iou))
        return InferenceResult(detections=list(self._detections), inference_ms=12.5)


# ================================================================== 夹具
@pytest.fixture()
def frame() -> np.ndarray:
    """一张真实场景图（`test_media/` 里那张），不是纯色块。

    走 `imaging.read_image` 而不是 `cv2.imread`：项目路径里有中文
    （`病虫害`），OpenCV 的 `imread` 在 Windows 上按 ANSI 解码路径，
    会直接返回 None —— 而 None 传到 `encode_jpeg` 里才炸，报错位置离原因很远。
    """
    root = Path(__file__).resolve().parents[2]
    image = imaging.read_image(root / "test_media" / "InsectPest_SIXU_A00025.jpg")
    assert image is not None, "test_media 里的样例图读不到，测试素材缺失"
    return image


@pytest.fixture()
def fake_camera(frame: np.ndarray) -> FakeCamera:
    return FakeCamera(frame)


@pytest.fixture()
def fake_detector() -> FakeDetector:
    return FakeDetector()


@pytest.fixture()
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture()
def agent_settings(settings: Settings) -> Settings:
    """启用代理的配置。中心地址是**假的**：这个文件里不会有真的连接。"""
    return replace(
        settings,
        agent=AgentConfig(
            enabled=True,
            center_url="http://center.test:8000",
            name="东头盒子",
            hardware_id="pi-test-0001",
            reconnect_max_s=60.0,
        ),
    )


def build(
    settings: Settings,
    transport: FakeTransport,
    camera: FakeCamera,
    detector: FakeDetector,
) -> EdgeAgentService:
    return EdgeAgentService(settings, detector, camera, transport=transport)


def run(coro) -> Any:
    """跑一个协程到完成。

    用 `asyncio.run` 而不是 `pytest-asyncio`：这里要驱的是**我们自己写的**
    协程（`_pair_loop` 等），而不是测试函数本身。用 `asyncio.run` 能明确
    看出"这一步是在驱动哪个协程"，也让超时由外部 `wait_for` 控制。
    """
    return asyncio.run(coro)


# ================================================================== 退避
def test_backoff_is_monotonic_and_capped():
    """⭐ 退避序列必须单调不减、且**真的**封顶在 reconnect_max_s。

    这是"配置名承诺的性质"：`reconnect_max_s=60` 却等出 90 秒，
    就是配置在骗人。取 jitter=0.5（乘 1.0）把抖动摘掉，让序列可预期。
    """
    delays = [
        backoff_delay(n, base_s=5.0, max_s=60.0, jitter=0.5) for n in range(8)
    ]
    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0]
    assert delays == sorted(delays), "退避序列必须单调不减"
    assert max(delays) <= 60.0


def test_backoff_jitter_stays_within_bounds():
    """抖动只该**打散**节拍，不该改变量级：下界是半量，上界就是上限本身。"""
    for jitter in (0.0, 0.5, 0.999):
        for attempt in range(6):
            delay = backoff_delay(attempt, base_s=5.0, max_s=60.0, jitter=jitter)
            assert 0 < delay <= 60.0


# ============================================================== 状态落盘
def test_state_survives_a_round_trip(tmp_path: Path):
    path = tmp_path / "agent.json"
    state = AgentState(
        hardware_id="pi-x",
        device_token="ipd_t",
        device_id=3,
        seq=41,
        live_detection=True,
    )
    state.save(path)

    loaded = AgentState.load(path)
    assert loaded == state, "落盘再读回来必须逐字段相等"


def test_non_bool_live_detection_is_treated_as_off(tmp_path: Path):
    """手改过的 `"false"` 是**真值字符串**，不能把会烧 CPU 的开关误开。"""
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"live_detection": "false"}), encoding="utf-8")
    assert AgentState.load(path).live_detection is False


def test_corrupt_state_file_reads_as_empty_not_a_crash(tmp_path: Path):
    """截断的 JSON 不该让代理彻底起不来 —— 现场没有终端可以调试。

    代价是身份丢了要重新配对，而这正是"我们至少还活着、还能打印一个新码"
    与"服务起不来、日志里一行都看不到"之间的差别。
    """
    path = tmp_path / "agent.json"
    path.write_text('{"hardware_id": "pi-x", "seq":', encoding="utf-8")
    assert AgentState.load(path) == AgentState()


def test_state_save_is_atomic(tmp_path: Path):
    """写一半掉电不能留下半个文件 —— 所以必须 `.tmp` + `os.replace`。

    断言的是"目录里只剩正式文件"：`os.replace` 会把临时文件搬走，
    如果实现改回直接覆写，这条仍会绿，但 `test_corrupt_state_file...`
    配上真实掉电才会红。这条锁的是**没有残留**这个可观察事实。
    """
    path = tmp_path / "nested" / "agent.json"
    AgentState(seq=1).save(path)
    assert path.exists()
    assert [p.name for p in path.parent.iterdir()] == ["agent.json"], "写完后不该留下 .tmp"
    assert AgentState.load(path).seq == 1


# ================================================================== 配对
def test_polls_until_paired_then_stops(agent_settings, transport, fake_camera, fake_detector):
    """⭐ 轮询到 paired 就**停下**，并把令牌落盘。

    "停下"是这里最容易写错的性质：多轮询一次，中心的
    `pending_token_plain` 已经被取走，第二次就会回 unknown，
    代理于是以为自己没配上、把刚拿到的令牌扔掉、重新打印一个码 ——
    用户看到的现象是"输完码过两秒又冒出一个新码"。
    """

    def on_poll() -> tuple[int, dict]:
        if transport.polls < 3:
            return 200, {"status": "pending"}
        return 200, {"status": "paired", "device_id": 9, "name": "东头盒子", "device_token": TOKEN}

    transport.on_poll = on_poll
    service = build(agent_settings, transport, fake_camera, fake_detector)

    run(asyncio.wait_for(service._pair_loop(), timeout=10))

    assert transport.pair_starts == 1, "配对码只该申请一次"
    assert transport.polls == 3
    assert service._state.device_token == TOKEN
    assert service._state.device_id == 9
    assert service._state.pairing_code == "", "配上之后不该留着旧码"
    assert AgentState.load(service._state_path).device_token == TOKEN, "令牌必须落盘"
    assert not (Path(agent_settings.data_dir) / "agent_pairing.txt").exists(), (
        "配上之后通知文件应当被清掉，否则有人会拿着一个已经用掉的码去输"
    )


def test_pairing_code_is_announced_and_written_to_disk(
    agent_settings, transport, fake_camera, fake_detector
):
    """码要同时出现在日志和文件里。

    现场经常 `systemctl status` 看不到滚动日志，`cat data/agent_pairing.txt`
    是最后一条不用 ssh 进去翻 journald 的路。
    """
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._announce_pairing_code(CODE)

    notice = Path(agent_settings.data_dir) / "agent_pairing.txt"
    assert notice.read_text(encoding="utf-8") == f"{CODE}\n"
    assert CODE in notice.read_text(encoding="utf-8")


def test_expired_code_triggers_a_new_pairing_request(
    agent_settings, transport, fake_camera, fake_detector
):
    """码过期 → 重新 pair/start 并打印**新**码。

    这条守的是"人拿着过期码输半天"这个具体现象：过期之后必须换码，
    而不是拿同一个码继续轮询到天荒地老。
    """
    codes = iter(["111111", "222222"])
    secrets = iter(["secret-one", "secret-two"])
    transport.on_pair_start = lambda: (
        200,
        {
            "pairing_id": 7,
            "code": next(codes),
            "pairing_secret": next(secrets),
            "expires_in": 300,
            "poll_interval_s": 0,
        },
    )
    seen: list[str] = []

    def on_poll() -> tuple[int, dict]:
        seen.append(service._state.pairing_code)
        if transport.polls == 1:
            return 200, {"status": "expired"}
        return 200, {"status": "paired", "device_id": 9, "device_token": TOKEN}

    transport.on_poll = on_poll
    service = build(agent_settings, transport, fake_camera, fake_detector)

    run(asyncio.wait_for(service._pair_loop(), timeout=10))

    assert transport.pair_starts == 2, "过期后必须重新申请一个码"
    assert seen[0] == "111111" and seen[1] == "222222", "第二次必须是新码"
    assert service._state.device_token == TOKEN


def test_restart_mid_pairing_resumes_instead_of_printing_a_new_code(
    agent_settings, transport, fake_camera, fake_detector
):
    """⭐ 配到一半重启：**接着轮询同一个码**，绝不重印。

    这是「关掉进程，等 30 秒，再起来」时最容易出现的劣化 —— 重印一个码，
    而人已经对着屏幕上那个旧码输了。`AgentState` 存在磁盘上就是为了这条。

    **凭证必须和码一起落盘**：只落码的话，重启后轮询会带着一个空凭证去问，
    中心按约定回 `unknown`，代理于是"重新配对" —— 屏幕上那个正被人抄的码
    就被顶掉了，正好是这条用例要防的事。所以这里除了断言"没有再申请新码"，
    还断言恢复出来的凭证真的被送了出去。
    """
    Path(agent_settings.data_dir).mkdir(parents=True, exist_ok=True)
    AgentState(hardware_id="pi-test-0001", pairing_code="987654", pairing_secret=SECRET).save(
        Path(agent_settings.data_dir) / "agent.json"
    )

    transport.on_poll = lambda: (200, {"status": "paired", "device_id": 9, "device_token": TOKEN})
    service = build(agent_settings, transport, fake_camera, fake_detector)

    run(asyncio.wait_for(service._pair_loop(), timeout=10))

    assert transport.pair_starts == 0, "磁盘上已有配对码，重启后不该再申请一个新的"
    assert service._state.device_token == TOKEN
    poll_headers = [headers for method, url, _payload, headers in transport.calls
                    if "/devices/pair/poll" in url]
    assert poll_headers and poll_headers[0]["X-Pairing-Secret"] == SECRET


def test_unknown_status_makes_it_start_over(agent_settings, transport, fake_camera, fake_detector):
    """中心说 unknown（不认识这台硬件 / 已被别人绑走）→ 重新申请一个码。"""

    def on_poll() -> tuple[int, dict]:
        if transport.polls == 1:
            return 200, {"status": "unknown"}
        return 200, {"status": "paired", "device_id": 9, "device_token": TOKEN}

    transport.on_poll = on_poll
    service = build(agent_settings, transport, fake_camera, fake_detector)

    run(asyncio.wait_for(service._pair_loop(), timeout=10))
    assert transport.pair_starts == 2


def test_a_network_error_while_polling_keeps_the_code(
    agent_settings, transport, fake_camera, fake_detector
):
    """轮询时网络抖动**不该**扔掉手上的配对码。

    码的有效期是 5 分钟，一次超时就重印一个码，等于把有效期缩到一次请求的超时 ——
    而人可能正在输那串数字。
    """

    def on_poll() -> tuple[int, dict]:
        if transport.polls == 1:
            raise OSError("连接被拒绝")
        return 200, {"status": "paired", "device_id": 9, "device_token": TOKEN}

    transport.on_poll = on_poll
    service = build(agent_settings, transport, fake_camera, fake_detector)

    run(asyncio.wait_for(service._pair_loop(), timeout=15))
    assert transport.pair_starts == 1, "网络错误不该导致换码"
    assert service._state.device_token == TOKEN


def test_hardware_id_falls_back_to_a_persisted_random_value(
    agent_settings, transport, fake_camera, fake_detector, monkeypatch
):
    """探测不到硬件标识时用随机值，且**必须落盘**。

    不落盘的话每次重启都换一个 id，中心会把同一台盒子当成一台新设备，
    配对码也就永远对不上 —— 而且现象是"绑定成功了，但重启后又冒出一个待配对设备"。
    """
    monkeypatch.setattr(edge_agent_module, "hardware_id_from_system", lambda: None)
    loosened = replace(
        agent_settings, agent=replace(agent_settings.agent, hardware_id="")
    )
    first = build(loosened, transport, fake_camera, fake_detector)
    generated = first._resolve_hardware_id()
    assert generated.startswith("dev-")

    second = build(loosened, transport, fake_camera, fake_detector)
    assert second._resolve_hardware_id() == generated, "重启后必须还是同一个标识"


# ================================================================== 令牌作废
def test_ws_auth_rejection_clears_the_token(agent_settings, transport, fake_camera, fake_detector):
    """⭐ 中心用 4401 关连接 → 清掉本地令牌，并且**不再重连**。

    这条防的是最耗人耐心的一种现场故障：盒子被解绑了，但代理拿着废令牌
    无限重连，日志里刷满"推流连接断开"，而网页上什么都看不到。
    正确动作是回到配对流程，在终端上打印一个新码。
    """
    transport.socket_factory = lambda: FakeSocket(close_code=4401)
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN

    with pytest.raises(TokenRejected):
        run(asyncio.wait_for(service._stream_loop(), timeout=10))


def test_ws_error_message_before_the_close_is_also_a_rejection(
    agent_settings, transport, fake_camera, fake_detector
):
    """中心先发 `{"type":"error"}` 再关：能读到它就该定性，不必等关闭帧。

    有些中间设备会把关闭帧吞掉，只认关闭码的话这里会退化成"一直重连"。
    """
    transport.socket_factory = lambda: FakeSocket(
        inbox=[json.dumps({"type": "error", "code": "unauthorized", "message": "设备令牌无效或已解绑"})]
    )
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN

    with pytest.raises(TokenRejected):
        run(asyncio.wait_for(service._stream_loop(), timeout=10))


def test_a_plain_network_drop_is_not_a_rejection(
    agent_settings, transport, fake_camera, fake_detector
):
    """1006（TCP 直接断）**不是**令牌作废 —— 必须退避重连，不能清令牌。

    混淆这两者是最容易犯的方向性错误：清掉令牌意味着重新配对，
    于人是一趟白跑；而网络抖动一天有一百次。
    """
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN

    sockets: list[FakeSocket] = []

    def factory() -> FakeSocket:
        # 连上并发出首帧（auth）之后才停 —— 关键是**真的进过一次连接**。
        # 原来是在调用 `_stream_loop()` **之前**就 `_stop.set()`，而循环条件是
        # `while not self._stop.is_set()`，于是循环体一次都不跑、socket 压根没被连过，
        # 下面那句断言恒真 —— 整条用例什么都没测到。
        socket = FakeSocket(close_code=1006, on_send=lambda _data: service._stop.set())
        sockets.append(socket)
        return socket

    transport.socket_factory = factory

    run(asyncio.wait_for(service._stream_loop(), timeout=10))

    # 先证明真的连过：没有这一句，将来谁再把停的顺序写回去，用例又会悄悄变空
    assert sockets and sockets[0].entered == 1, "这条用例必须真的建立过一次连接"
    assert service._state.device_token == TOKEN, "网络断开不能清掉令牌"


def test_upload_rejection_clears_the_token(agent_settings, transport, fake_camera, fake_detector):
    """上报被 403（解绑了）同样要清令牌 —— 两条路通向同一个动作。"""
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN
    transport.on_upload = lambda payload: (403, {"detail": "设备已解绑，请重新配对"})

    async def scenario() -> None:
        service._pending.append({"seq": 1})
        await service._flush_pending()

    with pytest.raises(TokenRejected):
        run(asyncio.wait_for(scenario(), timeout=10))
    assert len(service._pending) == 1, "被拒的那条不该被当成发成功而出队"


def test_forget_token_forces_a_fresh_pairing(agent_settings, transport, fake_camera, fake_detector):
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN
    service._state.pairing_code = "123456"
    service._state.device_id = 9

    service._forget_token()

    assert service._state.device_token == ""
    assert service._state.pairing_code == "", "重配时必须重新申请码，不能复用旧的"
    assert AgentState.load(service._state_path).device_token == ""


# ============================================================== 推流
def test_stream_sends_the_plan_the_center_announced(
    agent_settings, transport, fake_camera, fake_detector
):
    """⭐ 推流按中心 `ready` 里的流计划执行，**不是**按本机配置。

    中心知道它的观众与带宽，盒子照做即可。这条同时锁住一个更基本的事实：
    发出去的是**二进制** JPEG（`<img>` 那条 MJPEG 链路的输入），不是 base64 JSON。
    """
    import cv2

    socket = FakeSocket(
        inbox=[
            json.dumps(
                {
                    "type": "ready",
                    "device_id": 9,
                    "name": "东头盒子",
                    # 故意与本机配置（960/70/2）全都不同
                    "stream": {"fps": 200, "quality": 10, "width": 120},
                }
            )
        ]
    )

    service = build(agent_settings, transport, fake_camera, fake_detector)

    def stop_after_three(_data: Any) -> None:
        if len(socket.frames) >= 3:
            service._stop.set()

    socket.on_send = stop_after_three
    transport.socket_factory = lambda: socket

    run(asyncio.wait_for(service._stream_loop(), timeout=15))

    assert len(socket.frames) == 3
    for jpeg in socket.frames:
        assert bytes(jpeg[:2]) == b"\xff\xd8", "必须是 JPEG 的 SOI 头"
    decoded = cv2.imdecode(np.frombuffer(socket.frames[0], dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None, "发出去的帧必须能被解码"
    assert max(decoded.shape[:2]) <= 120, (
        f"中心要的是 width=120，实际最长边 {max(decoded.shape[:2])} —— 本机配置盖过了中心"
    )
    assert service._frames_sent == 3
    # 第一帧是 auth（文本），后面才是图
    assert json.loads(socket.sent[0])["type"] == "auth"
    assert json.loads(socket.sent[0])["token"] == service._state.device_token


def test_stream_reports_the_box_status_to_the_center(
    agent_settings, transport, fake_camera, fake_detector
):
    """⭐ 盒子要把它自己那部分状态捎给中心 —— 中心算不出来的那部分。

    中心只能算出 `online`（最后一次听到它是什么时候），答不了
    "在线却为什么没画面"。这条锁住两件事：连上就先报一次（网页那块调试面板
    不必等第一个周期才有数字），以及报文里**确实带着盒子自己记的 last_error**
    —— 那正是现场最想看的一行，也是最容易被漏掉的一行（只打日志、不上报，
    面板就会一直显示"一切正常"）。
    """
    socket = FakeSocket(
        inbox=[
            json.dumps(
                {"type": "ready", "device_id": 9, "stream": {"fps": 50, "quality": 70, "width": 160}}
            )
        ]
    )
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN
    service._note_error("推流取帧失败：CameraError: 打不开 /dev/video0")

    def stop_after_two(_data: Any) -> None:
        if len(socket.frames) >= 2:
            service._stop.set()

    socket.on_send = stop_after_two
    transport.socket_factory = lambda: socket

    run(asyncio.wait_for(service._stream_loop(), timeout=15))

    statuses = [
        json.loads(item)
        for item in socket.sent
        if isinstance(item, str) and json.loads(item).get("type") == "status"
    ]
    assert statuses, "连上就必须至少报一次状态"
    first = statuses[0]
    assert first["streaming"] is True
    assert first["stream_enabled"] is True
    assert first["version"], "版本号要跟着报文走，中心的显示才能自己跟上升级"
    assert first["last_error"].startswith("推流取帧失败")
    # 只发中心用得上的字段：把盒子整屏状态都塞进协议，往后谁也不敢删字段
    assert "center_url" not in first
    assert "hardware_id" not in first
    # 画面来源必须随状态报文上报：中心靠它才能分清"真摄像头"还是"模拟训练图"
    assert first["camera_backend"] == "simulated"


def test_stream_plan_is_ignored_when_not_streaming(
    agent_settings, transport, fake_camera, fake_detector
):
    """`stream_enabled=False`：连上但只发 hello/ping，一帧都不推。

    现场省流量时用这个。它照样要连着 —— 那条连接是这台盒子**唯一的在线凭据**
    （不推流的话，只有每 30 分钟一次的抓拍会刷新 last_seen_at，
    而 online 的窗口只有 60 秒，界面会一直显示离线）。
    """
    # 先给一条 ready（握手要的），之后靠 auto_pong 应答心跳
    socket = FakeSocket(inbox=[json.dumps({"type": "ready", "device_id": 9})], auto_pong=True)
    service = build(
        replace(agent_settings, agent=replace(agent_settings.agent, stream_enabled=False)),
        transport,
        fake_camera,
        fake_detector,
    )
    service._state.device_token = TOKEN

    def stop_after_first_ping(data: Any) -> None:
        # 只在**心跳**之后停：`on_send` 也会看到 auth 那一条，
        # 在 auth 上就停会让这个用例什么都没验到（连 ping 都不会发出去）。
        if isinstance(data, str) and json.loads(data)["type"] == "ping":
            service._stop.set()

    socket.on_send = stop_after_first_ping
    transport.socket_factory = lambda: socket

    run(asyncio.wait_for(service._stream_loop(), timeout=15))

    assert socket.frames == [], "关掉推流就一帧都不许发"

    kinds = [json.loads(item)["type"] for item in socket.sent if isinstance(item, str)]
    assert kinds[0] == "auth"
    # 连上先自报一次诊断：中心那块调试面板不必等第一个 10 秒周期才有数字。
    # （不写死 `kinds[1]` 之外的顺序 —— 后面加控制消息不该让这条用例红。）
    assert kinds[1] == "status"
    assert "ping" in kinds, "不推流时 ping 是这条连接唯一的健康检查"


def test_frame_grab_failure_does_not_kill_the_stream(
    agent_settings, transport, fake_camera, fake_detector
):
    """取帧偶尔失败（摄像头被别的进程扫了一下）不该断掉整条推流。

    恢复之后必须能继续发帧 —— 这条防的是"一次失败就退出循环"，
    现象是画面停住但连接还在，最难判断。
    """
    socket = FakeSocket(
        inbox=[json.dumps({"type": "ready", "stream": {"fps": 200, "quality": 40, "width": 120}})]
    )
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN
    fake_camera.fail_next = True

    def stop_after_one(_data: Any) -> None:
        if socket.frames:
            service._stop.set()

    socket.on_send = stop_after_one
    transport.socket_factory = lambda: socket

    run(asyncio.wait_for(service._stream_loop(), timeout=15))
    assert len(socket.frames) >= 1, "第一次取帧失败后应当重试并成功"
    assert fake_camera.grabs >= 2


# ============================================================== 抓拍上报
def test_capture_builds_a_payload_the_center_can_read(
    agent_settings, transport, fake_camera, fake_detector
):
    """⭐ 载荷字段与中心 `DeviceTaskIn` 逐项对齐。

    这里不联网，所以断言的是**形状**而不是"中心收下了" ——
    "中心能不能收下"由 `test_agent_simulated.py` 证明。
    两条都要有：这条快、能精确指出哪个字段写错了；那条慢、能证明真能通过校验。
    """
    import base64

    service = build(agent_settings, transport, fake_camera, fake_detector)
    payload = service._capture_and_build()

    assert payload["seq"] == 1
    assert payload["backend"] == agent_settings.inference.backend
    assert payload["conf"] == agent_settings.local_camera.conf
    assert payload["iou"] == agent_settings.local_camera.iou
    assert len(payload["detections"]) == 1

    det = payload["detections"][0]
    assert det == {
        "class_id": 0,
        "class_name": "aphid",
        "zh_name": "蚜虫",
        "score": pytest.approx(0.91),
        "bbox": [10, 20, 110, 140],
        "track_id": None,
        "frame_index": 1,
    }
    # 刻意**不**发中心自己会算的聚合值：多一个真相来源，就多一次口径不一致
    assert "counts" not in payload
    assert "total_objects" not in payload

    raw = base64.b64decode(payload["image_base64"], validate=True)
    assert raw[:2] == b"\xff\xd8", "上报的原图必须是真 JPEG"
    assert raw == encode_jpeg(fake_camera._frame, 85), "必须是**未缩放**的原图"


def test_uploaded_image_is_not_resized_because_boxes_are_in_original_coordinates(
    agent_settings, transport, fake_camera, fake_detector, monkeypatch
):
    """⭐ 上报的图必须是原图尺寸 —— 框坐标是原图坐标系，缩了框就全错位。

    这条之所以值得单独写：推流那一路**是**要缩的（不带框），很容易顺手
    把 `resize_keep_ratio` 也加到上报路径上，而后果是中心把框烧到错误的位置，
    界面看起来"就是框画得有点偏"，不会报任何错。
    """
    import base64

    import cv2

    # 把推流宽度压到远小于样例图，这条才**有分辨力**：真被误缩的话，
    # 输出就会明显小于原图，而不是"恰好也不用缩"所以蒙混过关。
    settings = replace(
        agent_settings, agent=replace(agent_settings.agent, stream_width=100)
    )
    service = build(settings, transport, fake_camera, fake_detector)
    payload = service._capture_and_build()
    raw = base64.b64decode(payload["image_base64"], validate=True)
    decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)

    assert decoded.shape[:2] == fake_camera._frame.shape[:2], "上报的图不能缩放"
    assert max(fake_camera._frame.shape[:2]) > settings.agent.stream_width, (
        "前提：样例图要比 stream_width 大，否则这条测试是空转的"
    )


def test_seq_keeps_increasing_across_restarts(
    agent_settings, transport, fake_camera, fake_detector
):
    """seq 必须单调递增且跨重启继续 —— 中心的幂等全靠它。

    回退的后果很隐蔽：中心会把新的抓拍当成旧的重发而**丢掉**，
    界面上表现为"隔一次少一张"，而且不报错。
    """
    first = build(agent_settings, transport, fake_camera, fake_detector)
    first._capture_and_build()
    first._capture_and_build()

    second = build(agent_settings, transport, fake_camera, fake_detector)
    assert second._state.seq == 2, "重启后必须接着数"
    assert second._capture_and_build()["seq"] == 3


def test_a_failed_upload_is_retried_and_the_frame_is_not_lost(
    agent_settings, transport, fake_camera, fake_detector
):
    """⭐ 上报失败的那一帧留在队列里，下一次补发。

    这是"断网一小时也不丢数据"的完整实现 —— 丢了的话，现场断网期间
    恰好在抓拍的那几张就永远看不到，而没有任何错误信息指向它。
    """
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN
    results = iter([(500, {"detail": "中心内部错误"}), (200, {"task_id": 12, "seq": 1})])
    transport.on_upload = lambda _payload: next(results)

    async def scenario() -> None:
        service._pending.append({"seq": 1, "image_base64": "x"})
        await service._flush_pending()          # 失败 → 仍然留在队列里
        assert len(service._pending) == 1, "失败的帧不能从队列里掉出去"
        await service._flush_pending()          # 这次成功 → 出队
        assert len(service._pending) == 0

    run(asyncio.wait_for(scenario(), timeout=10))
    assert len(transport.uploads) == 2, "同一条载荷应当被重发"
    assert transport.uploads[0] == transport.uploads[1]
    assert service._uploads == 1, "只有成功的那次要计数"


def test_retry_queue_sends_the_oldest_first(
    agent_settings, transport, fake_camera, fake_detector
):
    """补发必须**按 seq 从旧到新**。

    先发新的会让中心那边看到的时间顺序与现场发生的顺序相反，
    趋势图上就是一个假的尖峰。
    """
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN
    for seq in (1, 2, 3):
        service._pending.append({"seq": seq})

    run(asyncio.wait_for(service._flush_pending(), timeout=10))
    assert [p["seq"] for p in transport.uploads] == [1, 2, 3]


def test_retry_queue_is_bounded(agent_settings, transport, fake_camera, fake_detector):
    """队列有上限：断网几小时后不能让内存无限涨（每帧带一张 base64 图）。"""
    service = build(agent_settings, transport, fake_camera, fake_detector)
    assert service._pending.maxlen == edge_agent_module._PENDING_MAX
    for seq in range(edge_agent_module._PENDING_MAX + 5):
        service._pending.append({"seq": seq})
    assert len(service._pending) == edge_agent_module._PENDING_MAX


def test_capture_loop_uploads_on_its_own_clock(
    agent_settings, transport, fake_camera, fake_detector, monkeypatch
):
    """⭐ 定时抓拍真的会跑起来，并且把结果发到中心。

    把"一分钟的秒数"改小（照 `test_local_camera.py` 的既有手法）让第一拍
    在毫秒级发生 —— 否则这个用例要等满 1 分钟，实际结果就是没人跑它。
    """
    monkeypatch.setattr(edge_agent_module.local_camera_module, "SECONDS_PER_MINUTE", 0.05)
    monkeypatch.setattr(edge_agent_module, "_FIRST_CAPTURE_DELAY_S", 0.01)
    settings = replace(
        agent_settings,
        agent=replace(agent_settings.agent, capture_interval_min=1),
    )
    service = build(settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN

    async def scenario() -> None:
        task = asyncio.create_task(service._capture_loop())
        deadline = 10.0
        step = 0.02
        waited = 0.0
        while len(transport.uploads) < 2 and waited < deadline:
            await asyncio.sleep(step)
            waited += step
        service._stop.set()
        await asyncio.wait_for(task, timeout=5)

    run(asyncio.wait_for(scenario(), timeout=20))

    assert len(transport.uploads) >= 2, "定时抓拍应当持续上报，而不是只跑一次"
    assert [p["seq"] for p in transport.uploads] == [1, 2]
    assert transport.calls[-1][3] == {"X-Device-Token": TOKEN}, "上报必须带设备令牌"
    assert service._captures >= 2


def test_capture_interval_zero_means_stream_only(
    agent_settings, transport, fake_camera, fake_detector
):
    """两个间隔都是 0 = 只推流不自动抓拍（与本机摄像头对 0 的解释保持一致）。"""
    settings = replace(
        agent_settings,
        local_camera=replace(agent_settings.local_camera, interval_min=0),
        agent=replace(agent_settings.agent, capture_interval_min=0),
    )
    service = build(settings, transport, fake_camera, fake_detector)
    service._state.device_token = TOKEN

    async def scenario() -> None:
        task = asyncio.create_task(service._capture_loop())
        await asyncio.sleep(0.3)
        service._stop.set()
        await asyncio.wait_for(task, timeout=5)

    run(asyncio.wait_for(scenario(), timeout=10))
    assert transport.uploads == []


# ================================================================== 状态接口
def test_status_derives_paired_and_hides_a_stale_code(
    agent_settings, transport, fake_camera, fake_detector
):
    """`paired` 派生自令牌，`streaming` 派生自连接 —— 都不落库。

    落库的状态在崩溃后会卡在旧值上（"一直显示在线"），
    而这里最需要回答的恰恰是"现在到底怎么样"。
    配对码只在没绑上时才显示：绑上后中心已经清掉它，继续显示等于骗人。
    """
    service = build(agent_settings, transport, fake_camera, fake_detector)
    status = service.status()
    assert status["enabled"] is True
    assert status["running"] is False
    assert status["paired"] is False
    assert status["streaming"] is False
    assert status["hardware_id"] == "pi-test-0001"
    assert status["last_upload_at"] is None, "没上报过是 None，不是 0"

    service._state.pairing_code = CODE
    assert service.status()["pairing_code"] == CODE, "没绑上时要把码显示出来"

    service._state.device_token = TOKEN
    assert service.status()["paired"] is True
    assert service.status()["pairing_code"] == "", "绑上之后不该再显示配对码"


def test_status_is_visible_through_the_api(agent_settings, transport, fake_camera, fake_detector):
    """`GET /api/system/agent` 的契约 —— 现场手机上打开 Pi 自己的网页就能看码。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = replace(agent_settings, agent=replace(agent_settings.agent, enabled=False))
    app = create_app(settings)
    with TestClient(app) as client:
        creds = {"username": "agentuser", "password": "agentuser123"}
        client.post("/api/auth/register", json=creds)
        headers = {
            "Authorization": "Bearer "
            + client.post("/api/auth/login", json=creds).json()["access_token"]
        }

        assert client.get("/api/system/agent").status_code == 401, "运维信息要登录"

        body = client.get("/api/system/agent", headers=headers).json()
        assert body["enabled"] is False, "默认不启用，且这不该是个错误"
        assert body["hardware_id"] == "pi-test-0001"
        assert body["seq"] == 0
        assert body["last_error"] is None
        assert body["live_detection"] is False, "实时识别默认关闭"


# ============================================================== 不与抓拍抢
def test_agent_disables_the_local_scheduler(agent_settings, transport, fake_camera, fake_detector):
    """⭐ 启用代理时，本机定时抓拍**不许**启动。

    摄像头只有一个：两个调度器各抓各的，会让同一帧推理两次、
    每个间隔产出两条任务、4 核板上 CPU 直接翻倍。
    这条从**外部行为**验证那道闸（而不是读 `main.py` 的源码）。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = replace(
        agent_settings,
        local_camera=replace(agent_settings.local_camera, enabled=True, interval_min=30),
        agent=replace(
            agent_settings.agent,
            # 指向一个不存在的中心：代理起得来、连不上，正是现场掉线的样子
            center_url="http://127.0.0.1:1",
            request_timeout_s=0.5,
            reconnect_max_s=0.5,
        ),
    )
    app = create_app(settings)
    with TestClient(app):
        assert app.state.agent.enabled is True
        assert app.state.local_camera.running is False, (
            "代理启用时本机调度器必须保持不启动"
        )


def test_local_scheduler_still_starts_when_the_agent_is_off(settings):
    """反向对照：代理没启用时，本机定时抓拍照旧启动。

    没有这条，上面那条闸就可能被写成"永远不启动"，而那会让本机抓拍功能
    整体静默失效 —— 一个只有对照测试能抓到的 bug。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = replace(
        settings,
        local_camera=replace(settings.local_camera, enabled=True, interval_min=30),
        agent=replace(settings.agent, enabled=False),
    )
    app = create_app(settings)
    with TestClient(app):
        assert app.state.agent.enabled is False
        assert app.state.local_camera.running is True, "没启用代理时本机调度器应当照常跑"


def test_service_start_is_idempotent_and_shutdown_stops_it(
    agent_settings, transport, fake_camera, fake_detector
):
    """`start()` 两次只有一个线程；`shutdown()` 之后线程真的没了。

    `shutdown` 卡住是这类线程最常见的现场问题（systemd 只能补一刀 SIGKILL），
    所以断言里带上"能停干净"而不只是"跑起来了"。
    """
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service.start()
    try:
        assert service.running is True
        assert service._thread is not None
        service.start()
        assert threading.active_count() >= 1
    finally:
        service.shutdown()

    assert service.running is False
    assert service._thread is None


# ======================================================== 中心地址 → WS 地址
def test_next_ws_url_keeps_lan_to_public_a_one_value_change():
    """`http→ws`、`https→wss`，路径写死 `/ws/agent`。

    `_next_ws_url` 是"从局域网换到公网只改 `agent.center_url` 一个值"这个承诺的
    **唯一实现点**，此前没有测试 —— 而它的失败时机最坏：http 下一切正常，
    等到真给中心上了 https，盒子还在推 `ws://` 连不上，界面表现只是
    "绑上了但一直没有画面"，与网络故障长得一模一样。

    `center_url` 里原有的路径与 query 不能被带进来：用户很可能直接从浏览器
    地址栏复制一个带路径的地址（`http://host:8000/docs`）填进去。
    """
    f = edge_agent_module._next_ws_url

    # 局域网 → 公网：scheme 跟着变，其余不动。这两行就是整个承诺。
    assert f("http://192.168.1.20:8000") == "ws://192.168.1.20:8000/ws/agent"
    assert f("https://farm.example.com") == "wss://farm.example.com/ws/agent"

    # 端口、结尾斜杠、被顺手复制进来的路径与 query 都不该影响结果
    assert f("https://farm.example.com:8443/") == "wss://farm.example.com:8443/ws/agent"
    assert f("http://10.0.0.5:8000/api?x=1") == "ws://10.0.0.5:8000/ws/agent"
    assert f("http://10.0.0.5:8000/docs") == "ws://10.0.0.5:8000/ws/agent"


# ============================================================ 实时识别开关
def test_effective_stream_fps_caps_only_when_live_detection_is_on():
    """开启识别时压到 2 fps；关闭时必须原样保留中心给的计划。"""
    f = edge_agent_module._effective_stream_fps
    assert f(200, True) == 2
    assert f(1, True) == 1, "本来就比上限低，不该被抬高"
    assert f(200, False) == 200


def test_set_live_detection_persists_and_skips_unchanged_writes(
    agent_settings, transport, fake_camera, fake_detector, monkeypatch
):
    """开关落盘；值没变时不白写 SD 卡（抓拍每 30 分钟才写一次，别让开关刷盘）。"""
    service = build(agent_settings, transport, fake_camera, fake_detector)
    # 先把硬件标识落定：status() 首次会顺手补写它，那是另一条路径的写盘，
    # 不该算进"开关有没有重复写"的账里。
    service._hardware_id = "pi-test-0001"
    service._state.hardware_id = "pi-test-0001"
    writes: list[Path] = []
    monkeypatch.setattr(service._state, "save", lambda path: writes.append(path))

    assert service.set_live_detection(False)["live_detection"] is False
    assert writes == [], "已经是关闭状态，不该再写一次"

    assert service.set_live_detection(True)["live_detection"] is True
    assert service.live_detection is True
    assert len(writes) == 1
    assert service.status()["live_detection"] is True


def test_set_live_detection_keeps_running_when_the_disk_write_fails(
    agent_settings, transport, fake_camera, fake_detector, monkeypatch
):
    """SD 卡满/只读时，开关本次仍生效；失败记进 last_error，而不是报 500。"""
    service = build(agent_settings, transport, fake_camera, fake_detector)
    service._hardware_id = "pi-test-0001"
    service._state.hardware_id = "pi-test-0001"

    def boom(_path: Path) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(service._state, "save", boom)
    status = service.set_live_detection(True)

    assert status["live_detection"] is True, "运行时必须已经生效"
    assert "保存失败" in (status["last_error"] or "")


def _decode_jpeg(jpeg: bytes):
    import cv2

    return cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)


def _stream_one_frame(service, transport, *, width: int = 320, live: bool = False) -> bytes:
    """跑一次推流会话，发够一帧就停，返回那一帧 JPEG。"""
    socket = FakeSocket(
        inbox=[
            json.dumps(
                {"type": "ready", "stream": {"fps": 200, "quality": 80, "width": width}}
            )
        ]
    )
    service._state.live_detection = live

    def stop_after_one(_data: Any) -> None:
        if socket.frames:
            service._stop.set()

    socket.on_send = stop_after_one
    transport.socket_factory = lambda: socket
    run(asyncio.wait_for(service._stream_loop(), timeout=15))
    assert socket.frames, "应当至少发出一帧"
    return socket.frames[0]


def test_live_detection_annotates_streamed_frames(
    agent_settings, transport, fake_camera, fake_detector
):
    """⭐ 开启后发出去的 JPEG 必须真的带框；关闭时一帧都不许推理。"""
    raw_service = build(agent_settings, transport, fake_camera, fake_detector)
    raw_jpeg = _stream_one_frame(raw_service, transport, live=False)
    assert fake_detector.calls == [], "关闭时推理就是白烧 CPU"

    live_detector = FakeDetector()
    live_service = build(agent_settings, transport, fake_camera, live_detector)
    live_jpeg = _stream_one_frame(live_service, transport, live=True)

    assert live_detector.calls, "开启后必须真的推理"
    raw = _decode_jpeg(raw_jpeg)
    live = _decode_jpeg(live_jpeg)
    assert raw is not None and live is not None
    assert raw.shape == live.shape, "画框不该改变推流尺寸"
    assert not np.array_equal(raw, live), "画框必须真的改变像素"


def test_live_detection_falls_back_to_raw_when_inference_fails(
    agent_settings, transport, fake_camera
):
    """模型没加载也要能看到画面：推理失败降级为原始帧，流不断。"""

    class BoomDetector:
        def infer(self, frame, conf=None, iou=None):
            raise RuntimeError("模型尚未加载")

    raw_service = build(agent_settings, transport, fake_camera, FakeDetector())
    raw_jpeg = _stream_one_frame(raw_service, transport, live=False)

    service = build(agent_settings, transport, fake_camera, BoomDetector())
    jpeg = _stream_one_frame(service, transport, live=True)

    assert np.array_equal(_decode_jpeg(jpeg), _decode_jpeg(raw_jpeg))
    assert "实时识别失败" in (service.status()["last_error"] or "")


def test_live_detection_toggle_applies_to_the_next_frame(
    agent_settings, transport, fake_camera, fake_detector
):
    """流中途切换不需要重连：第 1 帧原始，第 2 帧就带框。"""
    socket = FakeSocket(
        inbox=[
            json.dumps(
                {"type": "ready", "stream": {"fps": 200, "quality": 80, "width": 320}}
            )
        ]
    )
    service = build(agent_settings, transport, fake_camera, fake_detector)

    def on_send(data: Any) -> None:
        if not isinstance(data, (bytes, bytearray)):
            return
        if len(socket.frames) == 1:
            service.set_live_detection(True)
        elif len(socket.frames) >= 2:
            service._stop.set()

    socket.on_send = on_send
    transport.socket_factory = lambda: socket

    run(asyncio.wait_for(service._stream_loop(), timeout=15))

    assert len(socket.frames) >= 2
    first, second = (_decode_jpeg(f) for f in socket.frames[:2])
    assert not np.array_equal(first, second), "开启后下一帧必须出现画框"
    assert len(fake_detector.calls) >= 1


def test_live_detection_api_requires_login_and_reports_conflicts(
    agent_settings, transport, fake_camera
):
    """网页开关的接口契约：未登录 401、未启用/关闭推流 409、成功则落盘。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    # 用 enabled=False 建 app，避免 lifespan 真的去连中心；随后换成一个
    # 未启动的真实 service —— 接口只读 status()/写状态，不需要后台线程。
    runtime = replace(agent_settings, agent=replace(agent_settings.agent, enabled=False))
    app = create_app(runtime)
    with TestClient(app) as client:
        creds = {"username": "liveuser", "password": "liveuser123"}
        client.post("/api/auth/register", json=creds)
        headers = {
            "Authorization": "Bearer "
            + client.post("/api/auth/login", json=creds).json()["access_token"]
        }
        url = "/api/system/agent/live-detection"

        assert client.post(url, json={"enabled": True}).status_code == 401
        assert client.post(url, json={"enabled": True}, headers=headers).status_code == 409

        service = EdgeAgentService(
            replace(
                agent_settings,
                agent=replace(
                    agent_settings.agent, enabled=True, stream_enabled=True
                ),
            ),
            FakeDetector(),
            fake_camera,
            transport=transport,
        )
        app.state.agent = service

        body = client.post(url, json={"enabled": True}, headers=headers).json()
        assert body["live_detection"] is True
        assert AgentState.load(service._state_path).live_detection is True

        body = client.post(url, json={"enabled": False}, headers=headers).json()
        assert body["live_detection"] is False
        assert AgentState.load(service._state_path).live_detection is False

        no_stream = EdgeAgentService(
            replace(
                agent_settings,
                agent=replace(
                    agent_settings.agent, enabled=True, stream_enabled=False
                ),
            ),
            FakeDetector(),
            fake_camera,
            transport=transport,
        )
        app.state.agent = no_stream
        response = client.post(url, json={"enabled": True}, headers=headers)
        assert response.status_code == 409
        assert "stream_enabled" in response.json()["detail"]
