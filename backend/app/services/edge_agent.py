# -*- coding: utf-8 -*-
"""树莓派边缘代理：把田里这台盒子的画面与识别结果**主动外拨**给中心。

## 为什么必须是外拨

这条链路上的每一次连接都由 Pi 发起 —— 配对、轮询、推流、上报任务，
**中心从不连进来**。这不是洁癖，是为了买一个具体的东西：现在跑在局域网，
以后把 `agent.center_url` 换成公网地址就完事，不需要 NAT 穿透、frp、内网映射，
也不用改一行前端（浏览器始终只跟中心说话）。中心主动连 Pi 的写法局域网能用，
换公网就得重写。

## 为什么跑在 Pi 的后端进程里，而不是独立进程

CSI/USB 摄像头**同一时刻只能被一个进程打开**。独立进程要么跟 Pi 自己的后端
抢设备，要么逼你永远别跑本机后端 —— 而 `deploy/insect-backend.service` 已经在跑了。
所以取帧收口到 `LocalCameraService.grab_frame()`：本机定时抓拍与推流共用同一个
`CameraSource`，只是节奏差得很远（2fps 取帧 vs 每 30 分钟一次推理）。

启用本代理时 `main.py` 的 lifespan **不会**再启动本机定时抓拍
（见那里的 `local_scheduler_active`），否则两个调度器各抓各的：同一帧推理两次、
每个间隔产出两条任务、4 核板上 CPU 直接翻倍。

## 明确不做：磁盘 outbox

上报失败只在**进程内**重试（`deque(maxlen=8)`）。进程崩掉时队列里那一帧就丢了。
可以接受：30 分钟才一张图，丢一帧 = 丢一个时间点，历史曲线照常；
而磁盘 outbox 要处理文件锁、损坏恢复、配额，为一个 30 分钟一次的场景不划算。
这个取舍是**刻意**的，写在这里免得后人以为是漏了。

## 依赖

WebSocket 客户端来自 `websockets`，它原本只是 `uvicorn[standard]` 的**传递**依赖，
`requirements-edge.txt` 里显式钉了 `websockets>=14`（面向的是新 asyncio 实现的
API：`connect` 收 `additional_headers`、关闭码在 `exc.rcvd.code`）。
这里惰性 import，缺了会给一条说清怎么装的中文错误 —— 代理是可选功能，
不该因为它把整个后端拖得起不来。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .. import __version__
from ..config import Settings
from ..models import utcnow
from . import imaging
# 走模块而不是 `from .local_camera import SECONDS_PER_MINUTE`：这两种写法对
# monkeypatch 的行为不一样 —— 绑定值会在 import 时就固化成一个副本，
# 以后有人按 tests/test_local_camera.py 的惯例去 patch `local_camera` 里那一个，
# 会发现"改了没用"。引用模块属性则两种 patch 都生效。
from . import local_camera as local_camera_module
from .hostinfo import describe_host

logger = logging.getLogger(__name__)

#: 与 ws/agent.py 的 `_CLOSE_AUTH_REQUIRED` 对齐。**只在协议这一侧写死一次**，
#: 两处都改才算改 —— 这个数字是两边唯一的约定。
_CLOSE_AUTH_REQUIRED = 4401

#: 中心回 ready 的等待上限。与 ws/agent.py 等 auth 的 15 秒同量级。
_AUTH_TIMEOUT_S = 15.0

#: 不推流时靠这个节奏发 hello 维持在线（中心的长连接会因此刷新 last_seen_at）
_IDLE_HEARTBEAT_S = 20.0

#: 自报诊断信息（`status` 报文）的间隔。10 秒与中心 `agent.heartbeat_s` 的默认值
#: 同量级：中心对这类报文的落库是节流的，发得更密也只是被丢掉。
_STATUS_INTERVAL_S = 10.0

#: 上报重试队列的深度。8 × ~30 分钟 ≈ 4 小时断网，再多就该怀疑是配置错了。
_PENDING_MAX = 8

#: 上报用的 JPEG 质量。比推流的 70 高：每 30 分钟才一张，
#: 而这张图是中心留档的"原图"，清晰度比带宽值钱。
_UPLOAD_QUALITY = 85

#: 推流实时识别时允许的最高帧率。树莓派 5 单帧 ONNX 约 0.25~0.4 秒，
#: 2 fps 已经接近预算；再高只会连续跳帧、把 CPU 占满。
_LIVE_DETECTION_MAX_FPS = 2

#: 实时识别失败日志的节流间隔。2 fps 下不节流会每秒刷两条 warning。
_LIVE_INFER_ERROR_LOG_INTERVAL_S = 30.0

#: 推流取帧失败日志的节流间隔。摄像头没插好时每一拍都失败，
#: 2 fps 就是每秒两条 —— 与上面同一个理由，只是这一条更常见。
_STREAM_ERROR_LOG_INTERVAL_S = 30.0


def _effective_stream_fps(fps: int, live_detection: bool) -> int:
    """开启实时识别时把推流压到固定上限。

    推理比取帧+编码慢得多；不压帧率的话，目标越高兴跳帧越频繁，
    画面反而更卡。中心给的计划帧率仍然生效，只是不会超过这个上限。
    """
    target = max(1, int(fps))
    return min(target, _LIVE_DETECTION_MAX_FPS) if live_detection else target

#: 退避基数（秒）。首个失败等 5 秒左右，之后翻倍到 reconnect_max_s。
_BACKOFF_BASE_S = 5.0

#: 首次抓拍的等待。刚绑上就想看到一张结果图 —— 等满一个间隔（默认 30 分钟）
#: 会让"到底绑上没有"当场无法验证。所以先拍一张，之后才按间隔来。
_FIRST_CAPTURE_DELAY_S = 3.0

#: 两次轮询之间的**下限**。中心会在 pair/start 里告诉我们它希望的间隔（2 秒），
#: 但那个值是**对端给的**：真收到 0 就变成全速轮询，把盒子的一个核烧掉。
#: 这是"从不信任对端给的节奏"的那道闸。测试里 monkeypatch 它来提速。
_MIN_POLL_INTERVAL_S = 0.2

_PAIRING_NOTICE_FILE = "agent_pairing.txt"


# ====================================================================== 错误
class AgentError(Exception):
    """本模块所有可预期错误的基类。"""


class TokenRejected(AgentError):
    """中心明确拒绝了设备令牌（4401）—— 令牌被解绑吊销了。

    与"网络断了"必须分开：网络断了要退避重连，令牌没了要**清掉本地令牌
    并跳回配对流程**。混成一种处理方式，就会让一台被解绑的盒子拿着废令牌
    永远重连下去，而人还在网页上等它出现。
    """


# ================================================================== 硬件标识
def _read_key(path: Path, key: str) -> str | None:
    """从 `Key : Value` 形式的文件里取一个值；读不到返回 None（不是抛）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(key):
            _, _, value = stripped.partition(":")
            value = value.strip()
            if value:
                return value
    return None


def hardware_id_from_system() -> str | None:
    """从系统里认出这台机器：`/proc/cpuinfo` 的 `Serial` → `/etc/machine-id`。

    为什么不用 hostname：树莓派默认 hostname 是 `raspberrypi`，同一片田里
    两台盒子会撞成同一个 id —— 而配对正是靠这个 id 认出"还是同一台盒子"。
    `Serial` 是每片 Pi 唯一的，重启、换网络、重刷系统都不变。
    退化到 machine-id 是为了让开发机上跑模拟代理也能有个稳定的身份。
    """
    serial = _read_key(Path("/proc/cpuinfo"), "Serial")
    if serial:
        return f"pi-{serial.lower()}"
    machine_id = _read_key(Path("/etc/machine-id"), "")
    if machine_id:
        return f"mid-{machine_id.lower()}"
    return None


# ================================================================== 状态落盘
@dataclass
class AgentState:
    """落盘在 `data_dir/agent.json` 的代理身份与进度。

    **为什么必须落盘**：配对码是*人*去网页上输入的，中间 Pi 可能重启。没有
    这份文件，重启就会打印一个新码，而人正对着屏幕上那个旧码输。
    `seq` 同理 —— 上报幂等全靠它单调递增，重启后必须接着数，不能从头来。

    `device_token` 也在这里：拿到之后就不再轮询配对接口了（中心那边
    `pending_token_plain` 只发一次，轮询也拿不到第二遍）。

    `live_detection` 是唯一一条**运行偏好**（网页上的「实时识别」开关）。
    放在这里而不是 config.yaml：配置文件要重启才生效，而这个开关应该
    点一下立刻生效；agent.json 已经有现成的原子写与容错读。
    """

    hardware_id: str = ""
    device_token: str = ""
    device_id: int | None = None
    pairing_code: str = ""
    #: `pair/start` 发下来的握手凭证，轮询时要带回去。**必须和配对码一起落盘** ——
    #: 落了码没落它，配到一半重启之后这台盒子就再也认不回自己的配对，
    #: 只能重新申请（而重新申请会把屏幕上那个正被人抄的码顶掉）。
    pairing_secret: str = ""
    seq: int = 0
    #: 是否在推流画面上本机推理并画框（网页开关，落盘以便重启后保留）
    live_detection: bool = False

    @classmethod
    def load(cls, path: Path) -> AgentState:
        """读状态文件。**坏文件当空状态**，而不是抛 —— 现场没有终端可调试，
        一个截断的 JSON 不该让代理彻底起不来（重新配对一遍即可恢复）。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = set(cls.__dataclass_fields__)
        data = {k: v for k, v in raw.items() if k in known}
        if data.get("device_id") is not None:
            try:
                data["device_id"] = int(data["device_id"])
            except (TypeError, ValueError):
                data["device_id"] = None
        if data.get("seq") is not None:
            try:
                data["seq"] = int(data["seq"])
            except (TypeError, ValueError):
                data["seq"] = 0
        # 只接受真正的 bool。手改过的 `"false"` 是**真值字符串**，
        # 直接塞进 dataclass 会把开关误开 —— 而这是会持续烧 CPU 的那一侧。
        if not isinstance(data.get("live_detection"), bool):
            data.pop("live_detection", None)
        return cls(**data)

    def save(self, path: Path) -> None:
        """原子写：先写 `.tmp` 再 `os.replace`。

        直接覆写的话，Pi 在写一半时掉电会留下一个截断的 JSON，
        下次启动就得重新配对、重新打印码 —— 而这正是这份文件要避免的事。
        """
        # 目录可能还没建（自定义 data_dir 时），而这里的失败后果很隐蔽：
        # 令牌存不下来 → 每次重启都重新配对、重印一个码，人对着旧码输半天。
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)


# ==================================================================== 传输层
class AgentTransport(Protocol):
    """代理与中心之间的两个动作：发一个 JSON 请求、开一条 WebSocket。

    抽成协议**只为一件事**：让配对 / 退避 / 重连这套状态机能在没有网络、
    没有 Pi 的机器上被测（见 `tests/test_agent_client.py` 的假传输）。
    生产实现就是下面的 `HttpTransport`，没有第二份实现。
    """

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        """返回 `(状态码, 解析后的 JSON 体)`。**4xx/5xx 不抛异常** —— 响应体里
        通常有可读的原因（"配对码已过期"），抛了就只能报一个光秃秃的 400。"""
        ...

    def connect_ws(self, url: str) -> Any:
        """返回一个 **async 上下文管理器**，`async with` 之后拿到连接对象。

        `websockets` 的 `connect(...)` 本身就同时是可等待对象和异步上下文
        管理器，直接返回它即可。
        """
        ...


def _decode_json(body: bytes) -> dict:
    """尽最大努力把响应体读成 dict。读不出来就当空 dict。

    错误响应的体不总是 JSON（代理服务器会插一页 HTML），
    这里不该因为体不合规就把一个本来能重试的网络错误变成崩溃。
    """
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class HttpTransport:
    """标准库 `urllib` 实现。

    刻意不用 requests/httpx：`requirements-edge.txt` 里没有它们，也不该为一个
    只用到三个接口的代理新增依赖（根 requirements 里那份 numpy/opencv 的
    钉版冲突还没解决，正是不想再添乱）。
    """

    def __init__(self, timeout_s: float = 20.0) -> None:
        self._timeout = timeout_s

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        data = None
        head = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            head["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return int(response.status), _decode_json(response.read())
        except urllib.error.HTTPError as exc:
            # HTTPError **本身也是一条响应**：4xx 的业务体里带着"配对码已过期"
            # 这种可读原因，顺手读出来交给调用方，排障时能省一轮猜测。
            # 连接层面的错误（URLError / timeout）照旧往外抛 —— 那些才是要退避的。
            with exc:
                return int(exc.code), _decode_json(exc.read())

    def connect_ws(self, url: str) -> Any:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - 只在依赖缺失时走到
            raise AgentError(
                "缺少 WebSocket 客户端库。树莓派上执行："
                "pip install 'websockets>=14'（它本来随 uvicorn[standard] 一起装，"
                "requirements-edge.txt 里已显式钉住）"
            ) from exc
        # ping 交给库自己做：它跑在自己的任务里，跟我们的取帧节奏无关，
        # 断线能被它更早发现（否则要等到下一次 send 才知道对面没了）。
        return connect(url, open_timeout=self._timeout, ping_interval=20, ping_timeout=20)


def _close_code(exc: Exception) -> int | None:
    """取对端关闭码；不是连接关闭类异常就返回 None。

    读 `exc.rcvd.code` 而不是 `exc.code`：后者自 websockets 13.1 起**已废弃**，
    会往日志里刷 DeprecationWarning —— pytest.ini 把它屏蔽了，但树莓派上的
    journald 不会，站内日志会被刷得没法看。
    `rcvd is None` 表示压根没收到关闭帧（TCP 直接断），那不是协议层的事。
    """
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is None:
        return None
    return int(getattr(rcvd, "code", 0)) or None


def as_auth_rejection(exc: Exception) -> TokenRejected | None:
    """把"对端用 4401 关掉了连接"翻译成 `TokenRejected`，别的异常返回 None。

    **判据是关闭码，不是异常类型名** —— websockets 的异常层次在版本间挪过位置，
    而关闭码是协议里写死的。中心在关之前还会发一条
    `{"type":"error","code":"unauthorized"}`，但那条是尽力而为：
    网络抖动时可能还没读到就断了，所以只认关闭码。
    """
    if _close_code(exc) == _CLOSE_AUTH_REQUIRED:
        return TokenRejected("中心拒绝了设备令牌（4401），本地令牌已作废")
    return None


def _next_ws_url(center_url: str) -> str:
    """由中心的 HTTP 地址推出 WS 地址：http→ws、https→wss。

    这样"从局域网换到公网"就真的只改 `agent.center_url` 一个值 ——
    换成 https 时 WS 自动跟着上 wss，不需要第二个配置项。
    """
    parsed = urllib.parse.urlsplit(center_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunsplit((scheme, parsed.netloc, "/ws/agent", "", ""))


# ====================================================================== 退避
def backoff_delay(attempt: int, *, base_s: float, max_s: float, jitter: float) -> float:
    """第 `attempt` 次失败（从 0 数）之后该等多少秒。

    `jitter` 传 `[0, 1)` 的随机数，测试里注入定值。抖动是**乘性**的
    （`× (0.5 + jitter)`），目的是打散多台盒子同时重连的节拍，
    而不是改变退避的量级，所以 `max_s` 在抖动**之后**封顶 ——
    `reconnect_max_s` 这个配置名承诺的是硬上限，能冒出 1.5 倍就不叫上限了。

    `jitter=0.5`（乘 1.0）时序列恰好是 `min(max_s, base * 2**attempt)`：
    单调不减、封顶。这条性质是可证伪的，测试直接锁它。
    """
    raw = base_s * (2 ** max(0, int(attempt)))
    return min(max_s, raw * (0.5 + jitter))


# ==================================================================== 代理本体
class EdgeAgentService:
    """把本机的摄像头画面与识别结果外拨给中心。

    线程模型：`start()` 起一个 daemon 线程，线程里跑一个**私有事件循环**
    （`asyncio.new_event_loop()`），与本项目"阻塞活扔线程"的既有约定一致。
    取帧与推理是阻塞调用，一律 `asyncio.to_thread` 丢出去，
    不占事件循环 —— 事件循环要留给"等 WS 可写"这种事。

    **不碰本地数据库**（所以构造它不需要 `session_factory`）：代理产出的
    结果属于中心，落在中心的 `tasks` 表里。盒子自己的库里不该出现这些记录 ——
    否则同一张图会在两边的历史页各看到一次，而"这台的"和"中心的"再也分不清。
    盒子上要看结果，就打开中心那个网页。

    环内结构：`_run` 是唯一的监督者。
    没有令牌就先跑 `_pair_loop`（直到拿到令牌），有令牌就跑
    `_capture_loop` + `_stream_loop` 两条循环；任一条抛 `TokenRejected`
    就清掉令牌、回到配对。网络类错误在**各循环内部**自己退避重试，
    不往上报 —— 上报就等于把两条无关的循环一起重启。
    """

    def __init__(
        self,
        settings: Settings,
        detector: Any,
        local_camera: Any,
        *,
        transport: AgentTransport | None = None,
    ) -> None:
        self._settings = settings
        self._detector = detector
        self._camera = local_camera
        self._state_path = Path(settings.data_dir) / "agent.json"
        self._state = AgentState.load(self._state_path)
        self._transport: AgentTransport = transport or HttpTransport(
            settings.agent.request_timeout_s
        )

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        # 重试队列：只存**待上报的载荷**，按 seq 顺序补发。
        # 中心 `device_ingest` 的唯一约束保证补发不会变成两条任务。
        self._pending: deque[dict] = deque(maxlen=_PENDING_MAX)
        self._poll_interval_s = 2.0
        self._hardware_id: str | None = None

        # 供 status() 与本地排障
        self._started_at: float | None = None
        self._connected = False
        self._frames_sent = 0
        self._skipped_frames = 0
        self._captures = 0
        self._uploads = 0
        self._last_upload_at: Any = None
        self._last_error: str | None = None
        self._last_error_at: Any = None
        # 实时识别失败的日志节流（见 _note_live_inference_error）
        self._live_infer_error_log_at: float | None = None
        # 推流取帧失败的日志节流（见 _log_throttled）
        self._stream_error_log_at: float | None = None

    # ------------------------------------------------------------ 生命周期
    @property
    def enabled(self) -> bool:
        """是否启用了代理。`main.py` 靠它决定要不要跳过本机定时抓拍。"""
        return bool(self._settings.agent.enabled)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def config(self):
        return self._settings.agent

    @property
    def live_detection(self) -> bool:
        """推流画面是否实时识别（网页开关，落盘在 agent.json）。"""
        with self._lock:
            return bool(self._state.live_detection)

    def set_live_detection(self, enabled: bool) -> dict[str, Any]:
        """开启/关闭推流画面的实时识别，并落盘。

        **写盘失败不改变运行结果**：SD 卡满或 data_dir 只读时，开关本次
        仍然生效，只是重启后会回到默认值。把失败记进 last_error，而不是
        让网页上的开关直接报 500 —— 用户要的是"现在生效"。
        """
        enabled = bool(enabled)
        with self._lock:
            if self._state.live_detection == enabled:
                return self.status()
            self._state.live_detection = enabled
            try:
                self._state.save(self._state_path)
            except OSError as exc:
                self._last_error = f"实时识别开关保存失败：{exc}"
                self._last_error_at = utcnow()
                logger.warning("实时识别开关未能落盘（运行时仍生效）：%s", exc)
        logger.info("实时识别已%s", "开启" if enabled else "关闭")
        return self.status()

    def start(self) -> dict[str, Any]:
        """起后台线程。已启用但没起线程时是幂等的。"""
        if not self.enabled:
            return self.status()
        with self._lock:
            if self.running:
                return self.status()
            self._stop.clear()
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._thread_main, name="edge-agent", daemon=True
            )
            self._thread.start()
        logger.info(
            "边缘代理已启动：中心 %s，硬件标识 %s，推流 %s",
            self.config.center_url,
            self._resolve_hardware_id(),
            "开" if self.config.stream_enabled else "关",
        )
        return self.status()

    def shutdown(self, timeout_s: float = 10.0) -> dict[str, Any]:
        """停后台线程。

        `self._stop` 是 `threading.Event` 而不是 `asyncio.Event`，因为要能被
        **另一个线程**置位；环内的等待一律走 `_sleep_or_stop` 轮询它，
        所以这里置位后最多过一个轮询周期（0.2 秒）就会醒。
        """
        with self._lock:
            thread, loop = self._thread, self._loop
        self._stop.set()
        if loop is not None:
            # 唤醒可能正卡在 sleep 里的循环
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:  # pragma: no cover - 环已关
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
            if thread.is_alive():
                # **不把 `_thread` 抹掉**：抹掉的话 `running` 会回答"没在跑"，
                # 而线程其实还在拨号 —— 于是 status() 和日志都说停干净了，
                # 实际没有，systemd 那边看到的是一个"停不下来又自称已停"的服务。
                # 停在 `_stop` 置位后的某次 IO 超时上是**正常**的，
                # 说真话比说好听的话有用。
                logger.warning(
                    "边缘代理线程在 %.0f 秒内没有退出（多半正卡在一次网络 IO 上），"
                    "保留 running=true 以便外部看到真实状态",
                    timeout_s,
                )
                return self.status()
        with self._lock:
            self._thread = None
        return self.status()

    # ------------------------------------------------------------ 线程入口
    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        with self._lock:
            self._loop = loop
        try:
            loop.run_until_complete(self._run())
        except Exception as exc:  # noqa: BLE001 - 线程里抛出去没人接
            self._note_error(f"代理主循环异常退出：{type(exc).__name__}: {exc}")
            logger.exception("边缘代理主循环异常退出")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
            with self._lock:
                self._loop = None
                self._connected = False
            logger.info("边缘代理已停止")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._state.device_token:
                    await self._pair_loop()
                    if self._stop.is_set():
                        break
                await self._work_until_token_rejected()
            except TokenRejected as exc:
                logger.warning("设备令牌被中心拒绝（%s）：清掉本地令牌，回到配对", exc)
                self._forget_token()
            except asyncio.CancelledError:  # pragma: no cover - 关停路径
                raise
            except Exception as exc:  # noqa: BLE001 - 监督者不许死
                self._note_error(f"代理主循环出错：{type(exc).__name__}: {exc}")
                logger.exception("边缘代理主循环出错，5 秒后重试")
                await self._sleep_or_stop(5.0)

    async def _work_until_token_rejected(self) -> None:
        """跑两条工作循环，直到其中一条**主动**要求重新配对。

        `_work_until_token_rejected` 这个名字就是这里的契约：两条循环内部都
        自己处理网络错误并退避重试，所以能从这里逃出来的只有
        `TokenRejected`（和关停）。这也解释了为什么用 FIRST_COMPLETED —
        正常情况下它永远不返回。
        """
        tasks = {asyncio.create_task(self._capture_loop(), name="agent-capture")}
        tasks.add(asyncio.create_task(self._stream_loop(), name="agent-stream"))
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc

    def _forget_token(self) -> None:
        """令牌作废：清掉它并强制重新配对（连配对码也一起清）。"""
        with self._lock:
            self._state.device_token = ""
            self._state.device_id = None
            self._state.pairing_code = ""
            self._state.pairing_secret = ""
            self._state.save(self._state_path)

    def _note_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
            self._last_error_at = utcnow()

    def _log_throttled(self, message: str) -> None:
        """按固定间隔记一条日志。

        推流是 2fps：摄像头坏着的时候每 0.5 秒一条 warning，
        几分钟就能把 journalctl 冲垮，真正有用的那几条反而被埋掉。
        错误本身不受影响 —— 每次都进 `_note_error`（页面上始终是最新的原因）。
        """
        now = time.monotonic()
        with self._lock:
            last = self._stream_error_log_at
            if last is not None and now - last < _STREAM_ERROR_LOG_INTERVAL_S:
                return
            self._stream_error_log_at = now
        logger.warning(message)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """睡 `seconds`，但 `shutdown()` 一置位就立刻醒。

        直接 `asyncio.sleep(1800)` 的话，收尾最多要等一整个抓拍间隔（30 分钟），
        systemd 只会把它当成"停不下来的服务"再补一刀。
        `threading.Event` 没法 await，所以只能按小步轮询 —— 0.2 秒的粒度
        对"等 30 分钟"这件事毫无成本。
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.2))

    # ------------------------------------------------------------ 地址/身份
    def _resolve_hardware_id(self) -> str:
        """稳定的硬件标识。优先级：配置 → 落盘 → 系统探测 → 新随机值（并落盘）。

        最后那一步的随机值必须落盘：不落盘的话每次重启都换一个 id，
        中心会把同一台盒子当成一台新设备，配对码也就永远对不上。
        """
        with self._lock:
            if self._hardware_id:
                return self._hardware_id
            configured = (self.config.hardware_id or "").strip()
            if configured:
                self._hardware_id = configured
            elif self._state.hardware_id:
                self._hardware_id = self._state.hardware_id
            else:
                detected = hardware_id_from_system() or f"dev-{uuid.uuid4().hex[:12]}"
                self._hardware_id = detected
                self._state.hardware_id = detected
                self._state.save(self._state_path)
            if self._state.hardware_id != self._hardware_id:
                self._state.hardware_id = self._hardware_id
                self._state.save(self._state_path)
            return self._hardware_id

    def _api(self, path: str) -> str:
        return f"{self.config.center_url.rstrip('/')}/api{path}"

    def _device_headers(self) -> dict[str, str]:
        return {"X-Device-Token": self._state.device_token}

    def _agent_name(self) -> str:
        return self.config.name or socket.gethostname()

    # ------------------------------------------------------------ 配对循环
    async def _pair_loop(self) -> None:
        """没有令牌时：申请配对码 → 打印 → 每 2 秒轮询，直到拿到令牌。

        **短轮询而不是 long-poll**：代理本身就是个还要重连、要重配对的循环，
        long-poll 会在网络超时之上再叠一层超时；而同步 `def` 路由下挂住一个
        线程池 worker 25 秒，在 4 核 Pi 上是实打实的开销。
        2 秒 × 5 分钟 ≈ 150 个请求，局域网上不算什么。long-poll 留作以后的升级项。
        """
        attempt = 0
        while not self._stop.is_set():
            if not self._state.pairing_code:
                try:
                    await asyncio.to_thread(self._start_pairing)
                except Exception as exc:  # noqa: BLE001 - 网络问题，退避重试
                    await self._backoff(attempt, f"申请配对码失败：{exc}")
                    attempt += 1
                    continue
                attempt = 0

            try:
                result = await asyncio.to_thread(self._poll_pairing)
            except Exception as exc:  # noqa: BLE001 - 配对码还有效，退避重试
                await self._backoff(attempt, f"轮询配对结果失败：{exc}（配对码仍然有效）")
                attempt += 1
                continue

            attempt = 0
            status = str(result.get("status") or "")
            if status == "paired":
                self._on_paired(result)
                return
            if status == "pending":
                pass  # 正常，继续等
            elif status == "expired":
                logger.info("配对码已过期，重新申请一个")
                self._clear_pairing_attempt()
            else:  # unknown：中心不认识这台硬件，或它已经绑在别的账号下
                logger.warning(
                    "中心不认识这台硬件（或者它已经绑定了）。重新申请配对码；"
                    "如果网页上显示它已被绑定，请先在网页上解绑。"
                )
                self._clear_pairing_attempt()

            await self._sleep_or_stop(self._poll_interval_s)

    def _start_pairing(self) -> None:
        """`POST /api/devices/pair/start`，把码打印出来并落盘。"""
        host = describe_host()
        payload = {
            "hardware_id": self._resolve_hardware_id(),
            "name": self._agent_name(),
            "model": str(host.get("model") or "") or None,
            "agent_version": __version__[:32],
            "backend": str(self._settings.inference.backend)[:16],
        }
        status, body = self._transport.request_json(
            "POST", self._api("/devices/pair/start"), payload=payload
        )
        if status != 200 or not body.get("code"):
            raise AgentError(f"中心返回 HTTP {status}：{body.get('detail') or body}")
        # 中心没给凭证 = 它比这台盒子旧。这时轮询必然一直被回 `unknown`，
        # 坐等下去只会让人对着一个永远绑不上的码发呆，所以在第一次就明说。
        if not body.get("pairing_secret"):
            raise AgentError(
                "中心没有返回 pairing_secret —— 中心与代理的版本不一致，两边都要升级"
            )

        code = str(body["code"])
        with self._lock:
            self._state.pairing_code = code
            self._state.pairing_secret = str(body["pairing_secret"])
            self._state.device_id = body.get("pairing_id")
            self._state.save(self._state_path)
        reported = body.get("poll_interval_s")
        self._poll_interval_s = max(
            _MIN_POLL_INTERVAL_S, 2.0 if reported is None else float(reported)
        )
        self._announce_pairing_code(code)

    def _poll_pairing(self) -> dict:
        """`GET /api/devices/pair/poll`。返回中心给的 `{status, ...}`。

        凭证走**头**：中心只认 `X-Pairing-Secret`，`hardware_id` 从 query 走。
        这不是为了好看 —— query 会进访问日志，而凭证泄漏就等于配对能被旁人代领。
        """
        query = urllib.parse.urlencode({"hardware_id": self._resolve_hardware_id()})
        status, body = self._transport.request_json(
            "GET",
            self._api(f"/devices/pair/poll?{query}"),
            headers={"X-Pairing-Secret": self._state.pairing_secret},
        )
        if status != 200:
            raise AgentError(f"中心返回 HTTP {status}：{body.get('detail') or body}")
        return body

    def _on_paired(self, result: dict) -> None:
        token = str(result.get("device_token") or "")
        if not token:
            # 中心说 paired 却没给令牌 —— 只可能是实现坏了。
            # 当成网络错误重试（下一次轮询会再次拿到令牌，它的 TTL 由中心管）。
            raise AgentError("中心回了 paired 但没有带 device_token")
        with self._lock:
            self._state.device_token = token
            self._state.device_id = result.get("device_id")
            self._state.pairing_code = ""
            # 中心在交出令牌的那一刻已经把这枚凭证作废了，留着没有意义。
            self._state.pairing_secret = ""
            self._state.save(self._state_path)
        self._clear_pairing_notice()
        logger.info(
            "配对成功：设备 id=%s 名字=%s，令牌已落盘 %s",
            result.get("device_id"),
            result.get("name"),
            self._state_path,
        )

    def _clear_pairing_attempt(self) -> None:
        """把这一次配对整个丢掉：码和凭证是一对，不能只清其中一个。

        只清码的话，下一轮 `_start_pairing` 会顺手换掉凭证（那条路径是安全的），
        但反过来——只清凭证留着码——轮询就会对着一个已经不成立的配对一直问。
        所以这一对永远一起动。
        """
        with self._lock:
            self._state.pairing_code = ""
            self._state.pairing_secret = ""
            self._state.save(self._state_path)
        self._clear_pairing_notice()

    def _announce_pairing_code(self, code: str) -> None:
        """把配对码打进日志，并另存一份文本文件。

        用纯 ASCII 画框：CJK 是双宽字符，靠右补空格对齐的边框在终端里
        永远差几格；而 `═` 这类框线字在旧编码的控制台上可能直接编码失败。
        没有右边框就没有对齐问题，也就没有这两类坑。

        另存文件是为了 `systemctl status` 看不到滚动日志时还有地方能 `cat`。
        """
        box = (
            "\n"
            "============================================================\n"
            f"   设备配对码： {code}\n"
            "   去网页「我的设备 → 绑定设备」输入它完成绑定\n"
            "   有效期 5 分钟；过期后这里会自动打印一个新的\n"
            "============================================================\n"
        )
        logger.warning(box)
        try:
            path = Path(self._settings.data_dir) / _PAIRING_NOTICE_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{code}\n", encoding="utf-8")
        except OSError as exc:  # 写不进去也要继续，日志里那份仍然有效
            logger.warning("配对码通知文件写入失败（不影响配对）：%s", exc)

    def _clear_pairing_notice(self) -> None:
        try:
            (Path(self._settings.data_dir) / _PAIRING_NOTICE_FILE).unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 删不掉无关紧要
            pass

    # ------------------------------------------------------------ 抓拍上报
    async def _capture_loop(self) -> None:
        """定时抓拍并上报，直到 `TokenRejected` 或关停。

        间隔取 `agent.capture_interval_min`，为 0 时沿用
        `local_camera.interval_min`；两者都为 0 就只推流、不自动上报
        （与 `LocalCameraService` 对 0 的解释保持一致：0 = 不自动抓）。
        """
        configured = int(self.config.capture_interval_min) or int(
            self._settings.local_camera.interval_min
        )
        if configured <= 0:
            logger.info("未配置抓拍间隔（agent 与 local_camera 都是 0）：只推流，不自动上报")
            await self._sleep_forever()
            return

        interval_s = max(1, configured) * local_camera_module.SECONDS_PER_MINUTE
        logger.info("定时抓拍已启动，间隔 %d 分钟", configured)
        # 先来一张：刚绑上就想看到结果图，等满一个间隔会让"绑上没有"当场无法验证。
        await self._sleep_or_stop(_FIRST_CAPTURE_DELAY_S)
        while not self._stop.is_set():
            try:
                payload = await asyncio.to_thread(self._capture_and_build)
            except Exception as exc:  # noqa: BLE001 - 取帧/推理失败，下一轮再来
                self._note_error(f"抓拍失败：{type(exc).__name__}: {exc}")
                logger.warning("抓拍失败（下一轮重试）：%s", exc)
            else:
                if len(self._pending) == self._pending.maxlen:
                    # 队列满 = 已经积压了几小时的上报，再多存只会吃光内存。
                    self._pending.popleft()
                    logger.warning("上报队列已满，丢弃最旧的一条（seq=%s）", payload["seq"])
                self._pending.append(payload)
                await self._flush_pending()
            await self._sleep_or_stop(interval_s)

    async def _flush_pending(self) -> None:
        """按 seq 顺序补发队列。队列里的每一条都重发到成功为止。

        中途失败就**整个退出**、下一轮再从头试：跳过失败的先发后面的，
        会让中心看到的任务顺序与现场发生的顺序不一致。
        """
        while self._pending and not self._stop.is_set():
            payload = self._pending[0]
            try:
                await asyncio.to_thread(self._upload, payload)
            except TokenRejected:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络类问题，下一轮再补
                self._note_error(f"上报失败：{type(exc).__name__}: {exc}")
                logger.warning(
                    "上报 seq=%s 失败（队列里还有 %d 条待补发）：%s",
                    payload.get("seq"),
                    len(self._pending),
                    exc,
                )
                return
            self._pending.popleft()
            with self._lock:
                self._uploads += 1
                self._last_upload_at = utcnow()

    def _upload(self, payload: dict) -> None:
        """`POST /api/devices/tasks`。同步，由调用方丢线程。"""
        status, body = self._transport.request_json(
            "POST",
            self._api("/devices/tasks"),
            payload=payload,
            headers=self._device_headers(),
        )
        if status == 200:
            logger.info(
                "上报成功：seq=%s task=%s 检出=%s%s",
                payload.get("seq"),
                body.get("task_id"),
                body.get("total_objects"),
                "（中心已收过，幂等）" if body.get("duplicate") else "",
            )
            return
        if status in (401, 403):
            # 令牌被解绑吊销了。清掉令牌回配对 —— 别在这里退避重试，
            # 一个作废的令牌重试一万次也还是 403。
            raise TokenRejected(f"上报被拒（HTTP {status}）：{body.get('detail') or body}")
        raise AgentError(f"上报失败（HTTP {status}）：{body.get('detail') or body}")

    def _capture_and_build(self) -> dict[str, Any]:
        """取一帧 → 本机推理 → 组一个上报载荷。**同步**，由调用方丢线程。

        **不缩放这张图**：框坐标是**原图坐标系**，中心拿这张图去烧框。
        这里一缩，框就全部错位 —— 省几十 KB 不值得拿坐标正确性去换。
        （推流那一路开实时识别时也会缩，但它在缩放**之后**的同一个数组上
        推理画框，坐标天然一致；这条定时抓拍不同，中心要的是原图留档。）
        """
        frame, source_info = self._camera.grab_frame()
        backend = str(source_info["name"])
        conf = self._settings.local_camera.conf or self._settings.inference.conf
        iou = self._settings.local_camera.iou or self._settings.inference.iou

        result = self._detector.infer(frame, conf, iou)
        jpeg = imaging.encode_jpeg(frame, _UPLOAD_QUALITY)
        if jpeg is None:
            raise AgentError("JPEG 编码失败，这一帧不上报")

        with self._lock:
            self._state.seq += 1
            seq = self._state.seq
            # seq 落盘要在**上传之前**：重发靠的是内存队列，而上报幂等靠的是 seq
            # 只增不减。崩在中间只会留下一个空缺的 seq（无害），
            # 但 seq 若回退，中心就会把新结果当成旧结果的重发而丢掉。
            self._state.save(self._state_path)
            self._captures += 1

        detections = [
            {
                "class_id": int(det.class_id),
                "class_name": str(det.class_name)[:64],
                "zh_name": str(getattr(det, "zh_name", "") or "")[:64],
                "score": float(det.score),
                "bbox": [int(v) for v in det.bbox],
                "track_id": det.track_id,
                "frame_index": 1,
            }
            for det in result.detections
        ]
        payload = {
            "seq": seq,
            "captured_at": utcnow().isoformat(),
            "conf": float(conf),
            "iou": float(iou),
            "backend": str(self._settings.inference.backend)[:16],
            "source_name": f"树莓派摄像头（{backend}）"[:128],
            "inference_ms": round(float(result.inference_ms), 2),
            # 刻意不发 counts / total_objects：中心从 detections 自己就能算出来，
            # 发过去只会多一个可能与中心口径不一致的真相来源（见 schemas.DeviceTaskIn）。
            "detections": detections,
            "image_base64": base64.b64encode(jpeg).decode("ascii"),
        }
        logger.info("抓拍完成：seq=%s 检出=%d 耗时=%.0fms", seq, len(detections), result.inference_ms)
        return payload

    # ------------------------------------------------------------ 推流循环
    async def _stream_loop(self) -> None:
        """维持一条到中心的 WebSocket：推流 + 在线心跳。

        `stream_enabled=False` 时它也照连 —— 那条连接上的 `hello` 是这台盒子
        **唯一的在线凭据**（不推流的话，只有每 30 分钟一次的上报会刷新
        `last_seen_at`，而 `online` 的窗口只有 60 秒，界面会一直显示离线）。
        """
        url = _next_ws_url(self.config.center_url)
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._stream_session(url, self._state.device_token)
            except TokenRejected:
                raise
            except asyncio.CancelledError:  # pragma: no cover - 关停路径
                raise
            except Exception as exc:  # noqa: BLE001 - 网络问题，退避重连
                rejection = as_auth_rejection(exc)
                if rejection is not None:
                    raise rejection
                await self._backoff(attempt, f"推流连接断开：{exc}")
                attempt += 1
                continue
            attempt = 0
        with self._lock:
            self._connected = False

    async def _stream_session(self, url: str, token: str) -> None:
        """一次连接：握手 → 推流/心跳，直到断开。"""
        async with self._transport.connect_ws(url) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            ready = await self._recv_json(ws)
            if str(ready.get("type")) != "ready":
                # 中心会在 4401 关闭之前发一条 error —— 能读到它就直接定性，
                # 不必等关闭帧（有些中间设备会把关闭帧吞掉）。
                raise TokenRejected(f"中心没有放行：{ready.get('message') or ready}")
            plan = ready.get("stream") or {}
            with self._lock:
                self._connected = True
            logger.info(
                "推流已接入：设备 id=%s 名字=%s 中心给的流计划=%s",
                ready.get("device_id"),
                ready.get("name"),
                plan,
            )
            # 连上就先报一次：中心那边的调试面板不必等到第一个 10 秒周期才有数字。
            await self._send_status(ws)
            try:
                if self.config.stream_enabled:
                    await self._pump_frames(ws, plan)
                else:
                    await self._pump_heartbeat(ws)
            finally:
                with self._lock:
                    self._connected = False

    async def _recv_json(self, ws: Any) -> dict:
        """从连接上读一条 JSON（握手用 ready/error，心跳用 pong）。"""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise AgentError("中心在 15 秒内没有回应 ready") from exc
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise AgentError(f"中心的 ready 不是合法 JSON：{raw[:120]!r}") from exc
        return parsed if isinstance(parsed, dict) else {}

    async def _send_status(self, ws: Any) -> None:
        """发一条 `status` 控制报文：把这台盒子的状态捎给中心。

        中心自己算不出这些 —— 它只知道"最后一次听到盒子是什么时候"，
        答不了"在线但为什么没画面"。于是网页的设备详情页才有东西可显示。
        字段的清洗在中心的 `ws/agent.py::_parse_status`：那边对类型一律宽容，
        所以**这里可以放心地直接发**，旧中心收到未知类型也只是回一条 error。
        """
        await ws.send(json.dumps(self._status_message()))

    def _status_message(self) -> dict[str, Any]:
        """给中心的状态报文。

        刻意**只挑中心用得上的那几个**，而不是把 `status()` 整屏都发出去：
        报文里混进 `center_url` 之类只属于盒子本机的东西，既没用，
        也会让协议越往后越难改（谁也不敢删字段）。
        """
        with self._lock:
            cam = self._camera.status()
            return {
                "type": "status",
                # 版本号跟着报文走，中心的显示会自己跟上 —— 这套系统是靠
                # "拷文件夹 + 重启"升级的，不该指望重新配对才更新版本。
                "version": __version__,
                "streaming": self._connected,
                "stream_enabled": bool(self.config.stream_enabled),
                "live_detection": bool(self._state.live_detection),
                "frames_sent": self._frames_sent,
                "uploads": self._uploads,
                "pending_uploads": len(self._pending),
                "last_error": self._last_error,
                "last_error_at": _iso_or_none(self._last_error_at),
                # 画面来源：中心靠它才能分清"真摄像头"还是"模拟训练图"。
                # 这是本次修复的核心字段——此前盒子静默回退到 simulated 时，
                # 网页上看起来一切正常，用户根本无从判断画面是真是假。
                "camera_backend": cam.get("backend"),
            }

    def _grab_and_encode_stream_frame(
        self, width: int, quality: int, annotate: bool
    ) -> bytes | None:
        """取一帧、缩到推流宽度、（可选）推理画框，再编码成 JPEG。

        **先缩放再推理**：检测框来自送进 `infer` 的数组，而画框也画在同一个
        数组上，坐标天然一致，不需要任何换算。默认推流宽 960，模型本来就会
        letterbox 到 640，精度代价很小。

        **推理失败只降级，不掐流**：现场最怕"模型没加载，连画面也没了"。
        """
        frame, _info = self._camera.grab_frame()
        image = imaging.resize_keep_ratio(frame, width)
        if annotate:
            conf = self._settings.local_camera.conf or self._settings.inference.conf
            iou = self._settings.local_camera.iou or self._settings.inference.iou
            try:
                result = self._detector.infer(image, conf, iou)
                image = imaging.draw_detections(image, result.detections)
            except Exception as exc:  # noqa: BLE001 - 实时识别失败不该中断直播
                self._note_live_inference_error(exc)
        return imaging.encode_jpeg(image, quality)

    def _note_live_inference_error(self, exc: Exception) -> None:
        """记录实时识别失败，但按固定间隔节流日志。

        2 fps 下每帧都失败的话，不节流就是每秒两条 warning，把 journalctl
        刷满反而看不见真正的原因。
        """
        now = time.monotonic()
        with self._lock:
            if (
                self._live_infer_error_log_at is not None
                and now - self._live_infer_error_log_at < _LIVE_INFER_ERROR_LOG_INTERVAL_S
            ):
                return
            self._live_infer_error_log_at = now
            self._last_error = (
                f"实时识别失败，已降级为原始画面：{type(exc).__name__}: {exc}"
            )
            self._last_error_at = utcnow()
        logger.warning("实时识别失败，已降级为原始画面：%s", exc)

    async def _pump_frames(self, ws: Any, plan: dict) -> None:
        """按节拍取帧、编码、发送。

        **节拍对齐到计划时刻，而不是"干完活再睡一个间隔"**：后者会把取帧+编码的
        耗时叠加到间隔上，实际帧率永远低于目标，且越慢越低。
        干不完就直接**跳帧**并把落后一笔勾销（不补发）—— 排队只会让观众看到
        越来越旧的画面，比掉帧糟得多。这是唯一的丢帧策略，也是对的。

        `ready` 里的流计划**优先于本地配置**：中心知道它的观众要什么，
        也知道自己的带宽；盒子照做即可（不同则记一条日志，便于排障）。

        网页开关打开时，每帧先在本机推理、画框再编码；此时目标帧率被压到
        `_LIVE_DETECTION_MAX_FPS`（2），避免推理耗时把发送拖成积压。
        开关**每帧重读**，切换后下一帧生效，不需要断开重连。
        """
        config = self.config
        plan_fps = max(1, int(plan.get("fps") or config.stream_fps))
        quality = int(plan.get("quality") or config.stream_quality)
        width = max(1, int(plan.get("width") or config.stream_width))
        local = (config.stream_fps, config.stream_quality, config.stream_width)
        if (plan_fps, quality, width) != local:
            logger.info(
                "采用中心的流计划 %s（本机是 %s），差异属预期",
                (plan_fps, quality, width),
                local,
            )

        next_at = time.perf_counter()
        measured_at = next_at
        measured_frames = 0
        last_live: bool | None = None
        next_status_at = time.perf_counter() + _STATUS_INTERVAL_S
        while not self._stop.is_set():
            # 每帧重读开关与目标帧率：网页上切换后下一帧就生效。
            live = self.live_detection
            fps = _effective_stream_fps(plan_fps, live)
            if last_live is not None and live != last_live:
                if live:
                    logger.info(
                        "推流实时识别已开启：每帧在本机推理并画框，推流限制 %d fps"
                        "（中心计划 %d fps）",
                        fps,
                        plan_fps,
                    )
                else:
                    logger.info("推流实时识别已关闭：只推原始画面")
            last_live = live
            interval = 1.0 / fps

            next_at += interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # 落后了就跳帧，并把基准拉到"现在"，不试图追回积压的节拍
                next_at = time.perf_counter()
                with self._lock:
                    self._skipped_frames += 1

            try:
                # asyncio.to_thread 只接受位置参数，不能写 annotate=live。
                jpeg = await asyncio.to_thread(
                    self._grab_and_encode_stream_frame, width, quality, live
                )
            except Exception as exc:  # noqa: BLE001 - 摄像头偶尔抽风，下一拍再试
                # 必须进状态：这是现场唯一能回答"盒子在线却没画面"的地方。
                # 之前只打日志，`/api/system/agent` 的 last_error 一直是空的，
                # 摄像头从开机就没出过图也看不出来。
                self._note_error(f"推流取帧失败：{type(exc).__name__}: {exc}")
                self._log_throttled(f"推流取帧/编码失败（下一拍重试）：{exc}")
                await asyncio.sleep(interval)
                continue

            if jpeg is None:
                continue
            # 只发二进制帧，收的是控制 JSON —— 这个方向约定见 ws/agent.py。
            # 发送本身不被用户配置的 fps 之外的东西节流；真慢到写不动时，
            # TCP 背压会让 `send` 挂住，下一拍自然就跳了。
            await ws.send(jpeg)
            with self._lock:
                self._frames_sent += 1
            measured_frames += 1

            # 周期性自报诊断。**放在发帧之后**：画面优先，状态晚 10 秒无所谓。
            if time.perf_counter() >= next_status_at:
                await self._send_status(ws)
                next_status_at = time.perf_counter() + _STATUS_INTERVAL_S

            now = time.perf_counter()
            if now - measured_at >= 60.0:
                with self._lock:
                    skipped = self._skipped_frames
                logger.info(
                    "推流实际 %.1f fps（目标 %d，累计跳帧 %d）",
                    measured_frames / (now - measured_at),
                    fps,
                    skipped,
                )
                measured_at, measured_frames = now, 0

    async def _pump_heartbeat(self, ws: Any) -> None:
        """不推流时的在线心跳：`ping` → 等 `pong`。

        等 pong 不只是礼貌：它顺手成了这条连接的健康检查 ——
        `send` 成功但对面已经死了的情况下（半开连接），只有收到 pong 才知道
        链路还活着，否则这条"在线"会一直假报下去。
        """
        while not self._stop.is_set():
            await ws.send(json.dumps({"type": "ping"}))
            reply = await self._recv_json(ws)
            if str(reply.get("type")) != "pong":
                raise AgentError(f"心跳没等到 pong：{reply}")
            # 报一次诊断。**放在 pong 之后**：pong 是这条连接的健康检查，
            # 先确认链路还活着，再往上捎状态。
            await self._send_status(ws)
            await self._sleep_or_stop(_IDLE_HEARTBEAT_S)

    async def _sleep_forever(self) -> None:
        """什么也不做，直到关停（用于"只推流不抓拍"的配置）。"""
        while not self._stop.is_set():
            await self._sleep_or_stop(60.0)

    # ---------------------------------------------------------------- 退避
    async def _backoff(self, attempt: int, message: str) -> None:
        delay = backoff_delay(
            attempt,
            base_s=_BACKOFF_BASE_S,
            max_s=self.config.reconnect_max_s,
            jitter=random.random(),
        )
        self._note_error(message)
        logger.warning("%s；%.1f 秒后重试", message, delay)
        await self._sleep_or_stop(delay)

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict[str, Any]:
        """供 `GET /api/system/agent` 与本地排障。

        `paired` / `streaming` 都是**派生**的（有令牌 / 连接活着），不落库 ——
        落库的状态在崩溃后会卡在旧值上，而这里最需要的就是"现在到底怎么样"。
        `live_detection` 相反：它是**用户选择**，必须落盘，重启后保持不变。
        """
        with self._lock:
            paired = bool(self._state.device_token)
            return {
                "enabled": self.enabled,
                "running": self.running,
                "center_url": self.config.center_url,
                # 这里**当场解析**而不是只读缓存：未启用、还没 start() 的时候，
                # 恰恰最需要看到"这台机器认自己是谁"（判断硬件标识是不是预期的
                # 那一个）。返回空串会让人以为标识没派生出来。
                "hardware_id": self._resolve_hardware_id(),
                "name": self._agent_name(),
                "device_id": self._state.device_id,
                "paired": paired,
                "streaming": self._connected,
                "stream_enabled": bool(self.config.stream_enabled),
                "live_detection": bool(self._state.live_detection),
                # 配对码只在**还没绑上**的时候有意义：绑上之后它已经被中心清掉了，
                # 留在这里只会让人以为还能用。
                "pairing_code": "" if paired else self._state.pairing_code,
                "capture_interval_min": int(self.config.capture_interval_min)
                or int(self._settings.local_camera.interval_min),
                "seq": self._state.seq,
                "frames_sent": self._frames_sent,
                "skipped_frames": self._skipped_frames,
                "captures": self._captures,
                "uploads": self._uploads,
                "pending_uploads": len(self._pending),
                "last_upload_at": _iso_or_none(self._last_upload_at),
                "last_error": self._last_error,
                "last_error_at": _iso_or_none(self._last_error_at),
                "uptime_s": (
                    None if self._started_at is None else round(time.time() - self._started_at, 1)
                ),
            }


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:  # pragma: no cover - 只在类型写错时
        return str(value)
