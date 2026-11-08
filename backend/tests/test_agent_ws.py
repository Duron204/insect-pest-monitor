# -*- coding: utf-8 -*-
"""盒子推流通道 `/ws/agent`，以及浏览器侧看画面的 MJPEG 链路。

**不需要权重、不需要 torch、不需要树莓派** —— 这条链路上中心只做三件事：
验令牌、把最新一帧塞进 `FrameHub`、把帧吐给浏览器。一帧都不推理。

这里守住三件最容易写坏的事：

1. **帧得真的到得了浏览器**（`test_stream_delivers_a_frame_to_a_browser`）。
   它是全套里唯一一个真的去读无限流的用例 —— 而无限流不设硬超时就是挂死 CI，
   所以用 `asyncio.wait_for` 夹住它，不靠"应该会很快"。
2. **中间只留最新一帧，绝不排队**（`test_only_the_latest_frame_is_kept`）。
   这是 frame_hub 的地基性质，这里从盒子那一端再验一次它确实贯通了。
3. **票据只能看它自己那一路**（`test_stream_ticket_is_scoped_and_expires`）。
   票据是整条链路上唯一一处凭证会进 query string 的地方，
   所以"拿 A 的票看 B 的流"必须 401。
4. **解绑能把已经在放的那一路掐掉**（`test_an_open_stream_stops_when_the_device_is_unbound`）。
   开流时的归属校验只管"新点开的那一路"；已经连上的那一路读的是内存里的
   `frame_hub`，只有生成器自己定期复查才停得下来。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.models import Device, DeviceStatus
from app.routers import devices as devices_module
from app.security import decode_stream_ticket, new_stream_ticket
from app.ws import agent as agent_module

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_IMAGE = PROJECT_ROOT / "test_media" / "InsectPest_SIXU_A00025.jpg"

HARDWARE_ID = "pi-serial-ws"

#: 复查归属的周期。真实默认是 30 秒（`devices.STREAM_REVALIDATE_S`）；
#: 用例里压到几十毫秒 —— 这里要证的是"会不会去查、查到解绑会不会停"，
#: 不是"停得有多快"。
REVALIDATE_S = 0.05

#: 解绑**之前**先让流跑这么久，用来证明"设备还有效时复查是放行的"。
#: 它必须跨过至少一次 tick（默认 2fps → 0.5 秒一条），否则那句断言是空的 ——
#: 没有它，一个"开流后立刻自己断掉"的坏生成器也能让整条用例变绿。
ALIVE_WHILE_OWNED_S = 1.0


# ---------------------------------------------------------------- 夹具
class Box(NamedTuple):
    """一台已经绑定好、手上拿着令牌的盒子。"""

    token: str
    device_id: int
    auth: dict[str, str]      # 设备主人的用户令牌请求头


@pytest.fixture()
def box(client: TestClient, register_user) -> Box:
    return _bind_box(client, register_user("farmer"), HARDWARE_ID)


@pytest.fixture()
def jpeg() -> bytes:
    """一张真实 JPEG 的字节。用它而不是随便 `b"x"*100`，是为了让
    "拿错设备的那一帧"这类错误在字节层面就看得见。"""
    return SAMPLE_IMAGE.read_bytes()


def _bind_box(client: TestClient, auth: dict[str, str], hardware_id: str) -> Box:
    """配对 → 绑定 → 轮询取令牌。盒子在真实世界走的就是这三步。"""
    started = client.post(
        "/api/devices/pair/start", json={"hardware_id": hardware_id, "name": "东头盒子"}
    ).json()
    device = client.post(
        "/api/devices/bind", json={"code": started["code"]}, headers=auth
    ).json()
    token = client.get(
        "/api/devices/pair/poll",
        params={"hardware_id": hardware_id},
        headers={"X-Pairing-Secret": started["pairing_secret"]},
    ).json()["device_token"]
    assert token, "绑定之后第一次轮询必须拿到令牌"
    return Box(token=token, device_id=device["id"], auth=auth)


def _device_row(app, device_id: int) -> Device:
    session = app.state.session_factory()
    try:
        row = session.get(Device, device_id)
        session.expunge(row)
        return row
    finally:
        session.close()


def _connected(client: TestClient, box: Box):
    """连上并完成鉴权，返回一个已经可以推帧的会话上下文。"""
    return client.websocket_connect("/ws/agent")


def _auth(ws, token: str) -> dict:
    ws.send_json({"type": "auth", "token": token})
    ready = ws.receive_json()
    assert ready["type"] == "ready", ready
    return ready


def _barrier(ws) -> None:
    """让服务端把此前收到的消息**处理完**再往下走。

    WebSocket 没有回执：`send_bytes()` 返回只代表消息进了队列，服务端可能还没读。
    所以每处"推完帧立刻断言数据库"之前都要夹一条 ping/pong ——
    服务端是按顺序处理的，pong 到了就说明前面那几帧已经处理完了。
    """
    ws.send_json({"type": "ping"})
    assert ws.receive_json()["type"] == "pong"


def _await(predicate, timeout_s: float = 5.0, what: str = "条件") -> None:
    """等某个条件成立。

    服务端的断开收尾（`hub.forget`）跑在 TestClient 的 portal 线程上，
    退出 `with` 块并不保证它已经跑完 —— 所以这里等一小会儿，
    而不是直接断言（那会变成随机红的用例）。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{timeout_s} 秒内{what}始终不成立")


# ---------------------------------------------------------------- 鉴权
def test_first_message_must_be_auth(client: TestClient):
    """首条必须是 auth，而且要用**专门的状态码**关掉连接。

    4401 不是装饰：盒子收到它才知道"我的令牌不认了"，进而清掉本地令牌
    重新走配对（见 edge_agent.py）。回一个含糊的断开会让它一直重连重试。
    """
    with client.websocket_connect("/ws/agent") as ws:
        ws.send_json({"type": "ping"})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "auth_required"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4401


def test_a_binary_first_frame_does_not_crash_the_handshake(client: TestClient):
    """⭐ 首条消息是**二进制**帧时，必须是干净的 4400，而不是把 handler 打崩。

    这两样都不需要令牌：谁都能连上来先发一帧二进制。原先用 `receive_text()` 读
    首条消息，它会去取 `message["text"]` 抛 `KeyError`，而那个异常不在任何
    except 里 —— 结果是 ASGI 打一份带栈的日志，客户端却**永远等不到那个约定好的
    关闭码**，盒子那边只看到连接断了，就按网络故障一直重连，永远收不到
    "你该重新配对"这个信号。
    """
    with client.websocket_connect("/ws/agent") as ws:
        ws.send_bytes(b"\xff\xd8\xff\xe0this-is-a-jpeg-not-a-text-frame")
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()

    # 4400 而不是 4401：这压根不是在说"你的令牌不行"，是"你没在说我们的协议"。
    # 关键是**这个码真的传到了客户端**，而不是异常把连接掐断（掐断会变成 1006）。
    assert excinfo.value.code == 4400


def test_a_non_string_token_does_not_crash_the_handshake(client: TestClient):
    """`{"type":"auth","token":123}` 也必须干净地拒绝。

    不挡这个类型的话，一个非字符串会一路走到 `hash_device_token` 的 `.encode()`
    抛 `AttributeError` —— 同样是**无需令牌即可触发**的未捕获异常。
    """
    with client.websocket_connect("/ws/agent") as ws:
        ws.send_json({"type": "auth", "token": 123})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4401


def test_bad_token_is_rejected(client: TestClient):
    with client.websocket_connect("/ws/agent") as ws:
        ws.send_json({"type": "auth", "token": "ipd_not-a-real-token"})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4401


def test_missing_token_is_rejected(client: TestClient):
    with client.websocket_connect("/ws/agent") as ws:
        ws.send_json({"type": "auth"})
        assert ws.receive_json()["code"] == "unauthorized"


def test_unbound_device_is_told_to_repair(client: TestClient, box: Box):
    """解绑之后，盒子拿旧令牌来得到的仍然是 4401（而不是能悄悄继续推流）。

    这条顺带钉住 `unbind_device` 那个"故意留着 token_hash"的决定：
    留着 hash 才能走到"令牌有效但绑定没了"这一支，回一句明确的
    「设备令牌无效或已解绑」，而不是让盒子一脸茫然地反复重连。
    """
    with client.websocket_connect("/ws/agent") as ws:
        assert _auth(ws, box.token)["device_id"] == box.device_id

    assert client.post(f"/api/devices/{box.device_id}/unbind", headers=box.auth).status_code == 200

    with client.websocket_connect("/ws/agent") as ws:
        ws.send_json({"type": "auth", "token": box.token})
        assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4401


def test_ready_announces_the_stream_plan(client: TestClient, app, box: Box):
    """`ready` 要把推流参数告诉盒子 —— 它不需要自己猜中心要什么质量、多少帧。"""
    settings = app.state.settings
    with client.websocket_connect("/ws/agent") as ws:
        ready = _auth(ws, box.token)
    assert ready["device_id"] == box.device_id
    assert ready["name"] == "东头盒子"
    assert ready["stream"] == {
        "fps": settings.agent.stream_fps,
        "quality": settings.agent.stream_quality,
        "width": settings.agent.stream_width,
    }


# ---------------------------------------------------------------- 推帧
def test_frames_land_in_the_hub_and_mark_the_device_online(client: TestClient, app, box: Box):
    """一帧推上来，内存与数据库两侧都要有反应。"""
    hub = app.state.frame_hub
    payload = b"\xff\xd8\xff\xe0-not-a-real-jpeg-but-bytes-are-bytes"

    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_bytes(payload)
        _barrier(ws)

        frame = hub.latest(box.device_id)
        assert frame is not None
        assert frame.jpeg == payload
        # 中心**不校验 JPEG 内容**：它只是把字节原样转给浏览器。
        # 要验的是"我们送来什么就存什么"，所以这里刻意用非图片字节。

        row = _device_row(app, box.device_id)
        assert row.last_frame_at is not None
        assert row.last_seen_at is not None

        listed = client.get("/api/devices", headers=box.auth).json()
        assert listed[0]["online"] is True
        # 一帧算不出帧率（要两个样本），凭这条把"没测到就不编数"的契约钉住；
        # 真有帧率时的算法归 test_frame_hub.py 管，这里不重复。
        assert listed[0]["stream_fps"] == 0.0


def test_only_the_latest_frame_is_kept(client: TestClient, app, box: Box):
    """⭐ 推 5 帧之后内存里只有第 5 帧。

    `frame_count == 1` 是可证伪的那一条：谁把 hub 改成"别丢帧嘛，排个队"，
    这里立刻红。排队意味着观众看到的是几十秒前的画面，比丢帧糟糕得多。
    """
    hub = app.state.frame_hub
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        for i in range(5):
            ws.send_bytes(b"frame-%d" % i)
        _barrier(ws)

        assert hub.frame_count(box.device_id) == 1
        assert hub.latest(box.device_id).jpeg == b"frame-4"
        assert hub.seq_of(box.device_id) == 4
        # 这里刻意**不**断言 fps：这 5 帧是背靠背推的，算出来的帧率只反映
        # 线程调度（同一份代码两次运行分别得到 0.0 和 250.0），断言任何一个
        # 具体数字都只会得到一条随机红的用例。
        # 帧率算法归 test_frame_hub.py 用假时钟精确管，这里不越界 ——
        # 这个用例拥有的性质只有上面那三条。


def test_heartbeat_writes_to_disk_at_most_once_per_interval(client: TestClient, app, box: Box):
    """⭐ 连推多帧只落一次库 —— 这是在保护盒子那张 SD 卡。

    2fps 推流若每帧都写 `last_seen_at`，就是每秒两次写盘。SD 卡是被写坏的，
    不是被用坏的。所以 `last_seen_at` 只推进一次，后面的帧都得被节流挡掉。
    """
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_bytes(b"first")
        _barrier(ws)
        first_write = _device_row(app, box.device_id).last_seen_at
        assert first_write is not None

        for i in range(4):
            ws.send_bytes(b"later-%d" % i)
        _barrier(ws)

        row = _device_row(app, box.device_id)
        assert row.last_seen_at == first_write, "心跳没被节流，每帧都在写库"
        # 但内存里的画面必须是新的 —— 节流的是"落库"，不是"收帧"
        assert app.state.frame_hub.latest(box.device_id).jpeg == b"later-3"


def test_oversized_frame_is_dropped_and_the_stream_survives(
    client: TestClient, app, box: Box, monkeypatch
):
    """坏掉的盒子推巨帧不能把内存吃光，也不能带崩这一路。

    这里把上限调成 1KB 而不是真发 8MB —— 验的是"超限即丢"这条行为，
    阈值是个旋钮，没必要为它跑一次 8MB 的分配。
    """
    monkeypatch.setattr(agent_module, "_MAX_FRAME_BYTES", 1024)
    hub = app.state.frame_hub

    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_bytes(b"x" * 4096)
        ws.send_bytes(b"good")
        _barrier(ws)

        # 关键断言是 seq：超限那帧**连序号都没占**，说明它根本没进 hub。
        # 只断言 jpeg == b"good" 是不够的 —— 覆盖式写入下那本来也会成立。
        assert hub.seq_of(box.device_id) == 0
        assert hub.latest(box.device_id).jpeg == b"good"


def test_empty_binary_frame_is_ignored(client: TestClient, app, box: Box):
    hub = app.state.frame_hub
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_bytes(b"")
        _barrier(ws)
        assert hub.latest(box.device_id) is None


def test_hello_keeps_a_non_streaming_box_online(client: TestClient, app, box: Box):
    """不推视频的盒子靠 `hello` 维持在线（`agent.stream_enabled = false`）。

    这条钉住 presence.py 那个刻意的取舍：`online` **只看心跳、不看有没有画面**。
    要是谁把"必须有帧才算在线"加回去，一个只上报结果、CPU 全留给推理的盒子
    就会永远显示离线 —— 而它明明在正常工作。
    """
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "hello"})
        _barrier(ws)

        assert app.state.frame_hub.latest(box.device_id) is None, "没推帧就不该有帧"
        assert client.get("/api/devices", headers=box.auth).json()[0]["online"] is True


def test_unknown_control_message_reports_error(client: TestClient, box: Box):
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "definitely-not-a-real-type"})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "unknown_type"


def test_malformed_json_reports_error_and_keeps_the_link(client: TestClient, box: Box):
    """坏消息只该回一条错误，**不该把连接关掉** —— 一条脏消息不值得让盒子重连。"""
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_text("{ not json")
        assert ws.receive_json()["code"] == "bad_json"
        _barrier(ws)          # 连接仍然活着


def test_status_reports_the_camera_backend_to_the_center(client: TestClient, box: Box, app):
    """画面来源必须能从盒子传到中心、再经 REST 读回来。

    这正是「为什么画面是训练照片」那次排查里缺的那一环：盒子静默回退到
    simulated 时，中心原本根本不知道，于是网页上看着一切正常。加了这一列，
    面板才说得清画面是真是假。
    """
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "status", "camera_backend": "simulated", "streaming": True})
        _barrier(ws)

    row = _device_row(app, box.device_id)
    assert row.agent_camera_backend == "simulated"

    # REST 也要能把这一列读出来（前端面板就走这条路）
    view = client.get(f"/api/devices/{box.device_id}", headers=box.auth)
    assert view.status_code == 200
    assert view.json()["agent_camera_backend"] == "simulated"


def test_camera_backend_flips_trigger_an_immediate_write(client: TestClient, box: Box, app):
    """画面来源从真实摄像头翻到模拟图时，必须立刻落库，不等地跳窗口。

    排查「为什么画面是训练照片」时，这一跳正是最该被立刻看见的。
    """
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "status", "camera_backend": "picamera2"})
        _barrier(ws)
        assert _device_row(app, box.device_id).agent_camera_backend == "picamera2"

        ws.send_json({"type": "status", "camera_backend": "simulated"})
        _barrier(ws)
        assert _device_row(app, box.device_id).agent_camera_backend == "simulated"



def test_disconnect_forgets_the_frame_but_keeps_the_device_row(client: TestClient, app, box: Box):
    """断开就丢掉那一帧，但**数据库里的设备行照旧保留**。

    网络断了而已：绑定关系、历史记录都不该受影响。
    """
    hub = app.state.frame_hub
    with client.websocket_connect("/ws/agent") as ws:
        _auth(ws, box.token)
        ws.send_bytes(b"last-frame")
        _barrier(ws)
        assert hub.seq_of(box.device_id) == 0

    _await(lambda: hub.latest(box.device_id) is None, what="断开后画面被清掉")
    row = _device_row(app, box.device_id)
    assert row is not None
    assert row.status == DeviceStatus.ACTIVE
    assert row.owner_id is not None


# ---------------------------------------------------------------- 票据
def test_stream_ticket_is_scoped_and_expires(client: TestClient, app, register_user):
    """票据只在"这一台、这段时间"里有效。

    票据出现在 query string 里，这是整条链路上唯一一处凭证会进日志的地方，
    所以它的四个边界都得挡住：串设备、过期、拿用户 JWT 冒充、没有票据。
    """
    auth = register_user("farmer")
    first = _bind_box(client, auth, "pi-a")
    second = _bind_box(client, auth, "pi-b")

    ticket = client.post(f"/api/devices/{first.device_id}/stream-ticket", headers=auth).json()
    assert ticket["expires_in"] == 120

    # 先确认这枚票据本身是好的 —— 不然下面那些 401 什么都说明不了。
    # 这里不能真去读流：TestClient 会把响应体整个缓冲，无限流会直接挂死。
    # "正主能看"那一条由下面的 async 用例负责。
    secret = app.state.settings.resolved_jwt_secret
    assert decode_stream_ticket(ticket["ticket"], first.device_id, secret) is not None

    # ① 串设备：A 的票取 B 的流
    response = client.get(
        f"/api/devices/{second.device_id}/stream", params={"ticket": ticket["ticket"]}
    )
    assert response.status_code == 401

    # ② 过期：同一台设备、签名合法，只是已经过了 TTL
    user_id = decode_stream_ticket(ticket["ticket"], first.device_id, secret)
    expired, _ = new_stream_ticket(user_id, first.device_id, secret, ttl_s=-1)
    assert client.get(
        f"/api/devices/{first.device_id}/stream", params={"ticket": expired}
    ).status_code == 401

    # ③ 拿用户 JWT 冒充票据。没有这条，`purpose` 就白设了 ——
    # 12 小时的用户令牌能直接当两分钟的票据用，安全性等于没有。
    user_jwt = auth["Authorization"].removeprefix("Bearer ")
    assert client.get(
        f"/api/devices/{first.device_id}/stream", params={"ticket": user_jwt}
    ).status_code == 401

    # ④ 干脆不带票据
    assert client.get(f"/api/devices/{first.device_id}/stream").status_code == 422


def test_a_stream_ticket_is_not_a_login_token(client: TestClient, app, register_user):
    """⭐ 票据不能反过来当账号凭证用 —— 这条路上曾经真的有个洞。

    票据与用户令牌用**同一个密钥、同一个算法**签发，`sub` 也都是用户 id。
    之前两侧的检查是不对称的：`decode_stream_ticket` 会核对 `purpose`，
    所以用户 JWT 冒充票据会被挡住（上面那条用例的 ③）；但**用户侧那条路
    什么也没核对** —— 拿票据当 `Authorization: Bearer` 用，验签照样通过。

    后果不是"多看两眼摄像头"，而是"票据即账号"：票据按设计要进 query string
    （访问日志、浏览器历史、Referer），也就是说它**注定会泄漏**；
    而拿到它的人可以列出账号下所有设备、解绑、删任务 —— 一分钱的门槛都没有。
    修法是 `decode_access_token`：带 `purpose` 的令牌一律不认（默认收口）。

    这里守两条用户侧入口：HTTP 的 `get_current_user` 和 WebSocket 的 `_authenticate`。
    两个都得测 —— 它们是两份独立实现，只修一处是最容易犯的错。
    """
    auth = register_user("farmer")
    device = _bind_box(client, auth, HARDWARE_ID)
    ticket = client.post(
        f"/api/devices/{device.device_id}/stream-ticket", headers=auth
    ).json()["ticket"]

    # 先证明这枚票据**本身是好的**：不然后面那些 401 可能只是因为票据无效，
    # 那这条用例就什么都没证明。
    secret = app.state.settings.resolved_jwt_secret
    assert decode_stream_ticket(ticket, device.device_id, secret) is not None

    as_login = {"Authorization": f"Bearer {ticket}"}
    # ① 列出账号下的设备（读了哪些设备本身也是信息）
    assert client.get("/api/devices", headers=as_login).status_code == 401
    # ② 解绑 —— 破坏性操作，这是最该挡住的一个
    assert client.post(
        f"/api/devices/{device.device_id}/unbind", headers=as_login
    ).status_code == 401
    # ③ 顺带确认它没把设备怎么样：401 必须是"什么都没做"，不是"做完了才报错"
    owner_view = client.get(f"/api/devices/{device.device_id}", headers=auth)
    assert owner_view.status_code == 200
    assert owner_view.json()["status"] == "active"

    # ④ WebSocket 那条路是另一份实现（`ws/detection.py` 的 `_authenticate`），
    #    单独走一遍。两处都修了才算修，只修一处是最容易犯的错。
    with client.websocket_connect("/ws/detection") as ws:
        ws.send_json({"type": "auth", "token": ticket})
        assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4401


def test_stream_ticket_requires_ownership(client: TestClient, register_user):
    """别人连票据都领不到，更别说看画面。"""
    owner = register_user("farmer")
    intruder = register_user("intruder")
    device = _bind_box(client, owner, HARDWARE_ID)

    url = f"/api/devices/{device.device_id}/stream-ticket"
    assert client.post(url).status_code == 401
    assert client.post(url, headers=intruder).status_code == 404
    assert client.post(url, headers=owner).status_code == 200


def test_stream_refuses_a_ticket_after_unbind(client: TestClient, register_user):
    """解绑后，还没过期的票据也立刻作废 —— 开流时要再核对一次归属。"""
    auth = register_user("farmer")
    device = _bind_box(client, auth, HARDWARE_ID)
    ticket = client.post(
        f"/api/devices/{device.device_id}/stream-ticket", headers=auth
    ).json()["ticket"]

    client.post(f"/api/devices/{device.device_id}/unbind", headers=auth)

    assert client.get(
        f"/api/devices/{device.device_id}/stream", params={"ticket": ticket}
    ).status_code == 404


async def test_an_open_stream_stops_when_the_device_is_unbound(
    app, client: TestClient, box: Box, jpeg: bytes, monkeypatch
):
    """⭐ **已经在放**的那一路，解绑之后必须自己停下。

    上一条只挡得住"再点开一个新标签页"。已经连上的那一路走的是内存里的
    `frame_hub`：票据的 120 秒 TTL 帮不上忙（票据只在开流时看一眼），
    `hub.forget()` 也挡不住（盒子那条 WS 还连着，下一帧就把画面填了回来）。
    于是最坏的一种情形是：A 解绑、B 把同一台盒子绑走，**A 那个没关的页面
    继续看到 B 的摄像头画面**，直到他刷新。

    所以生成器每 `STREAM_REVALIDATE_S` 秒自己复查一次归属。这里把周期压到
    50 毫秒，先让它带着有效设备跑满一秒（证明复查确实在放行），再解绑。
    """
    monkeypatch.setattr(devices_module, "STREAM_REVALIDATE_S", REVALIDATE_S)
    app.state.frame_hub.publish(box.device_id, jpeg)
    ticket = client.post(
        f"/api/devices/{box.device_id}/stream-ticket", headers=box.auth
    ).json()["ticket"]

    async def unbind_after_a_while(closed: asyncio.Event) -> None:
        await asyncio.sleep(ALIVE_WHILE_OWNED_S)
        assert not closed.is_set(), "设备还没解绑，服务端不该挂断这一路"
        assert (
            client.post(
                f"/api/devices/{box.device_id}/unbind", headers=box.auth
            ).status_code
            == 200
        )

    code, closed = await _drive_mjpeg(
        app,
        f"/api/devices/{box.device_id}/stream",
        {"ticket": ticket},
        unbind_after_a_while,
    )

    assert code == 200, "开流那一刻设备还是好的 —— 所以停下不是开流校验干的"
    assert closed, "解绑之后服务端必须自己把这一路挂掉，而不是继续放别人的画面"




# ---------------------------------------------------------------- MJPEG 正路
def _asgi_scope(path: str, query: dict[str, str]) -> dict:
    """一条最小的 HTTP GET ASGI scope。下面两个驱动流的助手共用。"""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query).encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }


async def _read_one_frame(app, path: str, query: dict[str, str], needle: bytes,
                          timeout_s: float = 10.0) -> tuple[int, dict, bytes]:
    """自己驱动 ASGI，凑够一帧就停。返回 (状态码, 响应头, 收到的字节)。

    **为什么不能用现成的客户端**：`TestClient` 和 `httpx.ASGITransport`
    都会把整个响应体缓冲下来（前者写进 `BytesIO`，后者 `await self.app(...)`
    跑完才返回），而 MJPEG 是条永不结束的流 —— 用它们只会挂死。
    这里直接给 ASGI 三个 callable，读到针尖就 `done.set()`，然后把任务取消掉。

    `timeout_s` 是硬超时：真出问题时是"用例失败"，不是"CI 永远卡住"。
    """
    scope = _asgi_scope(path, query)
    status: dict = {}
    buffer = bytearray()
    done = asyncio.Event()
    #: 永不会被 set —— 我们从不主动断开，流的结束由 `done` 那一边说了算。
    never = asyncio.Event()

    async def receive():
        # ⚠️ **必须真的 await 一个东西**，不能直接 `return {...}`。
        # `StreamingResponse` 会并发起一个 `listen_for_disconnect` 任务，
        # 它 `while True: await receive()`；如果 receive 里一个 await 点都没有，
        # 这个循环就永远不会把控制权交还事件循环 —— 整个 loop 直接卡死，
        # 连 `wait_for` 的超时回调都触发不了（现象是 pytest 无声挂住）。
        # 等一个永不发生的断开，就既让出了控制权，又保持了"连接还在"的语义。
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
            status["headers"] = dict(message.get("headers", []))
        elif message["type"] == "http.response.body":
            buffer.extend(message.get("body", b""))
            if needle in buffer:
                done.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(done.wait(), timeout_s)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return status["code"], status["headers"], bytes(buffer)


async def _drive_mjpeg(app, path: str, query: dict[str, str], after_open,
                       timeout_s: float = 10.0) -> tuple[int, bool]:
    """开一路 MJPEG，跑完 `after_open`，再看**服务端自己会不会挂断**。

    返回 `(状态码, 是否已挂断)`。`after_open` 拿得到 `closed` 这个事件，
    好在中途断言"这会儿它还活着"。

    **怎么知道服务端挂了**：`StreamingResponse` 收尾时一定会发一条
    `more_body=False` 的 body，就是它。**不能改成 `await task`** ——
    Starlette 会并发起一个 `listen_for_disconnect`，它 `await receive()`；
    而我们那个 receive 是"等一个永不发生的断开"（见 `_read_one_frame`），
    所以任务本身永远等不到返回，只有 `task.cancel()` 能收场。
    """
    scope = _asgi_scope(path, query)
    status: dict = {}
    opened = asyncio.Event()
    closed = asyncio.Event()
    never = asyncio.Event()

    async def receive():
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
            opened.set()
        elif message["type"] == "http.response.body" and not message.get(
            "more_body", False
        ):
            closed.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(opened.wait(), timeout_s)
        await after_open(closed)
        await asyncio.wait_for(closed.wait(), timeout_s)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"{timeout_s} 秒内服务端既没开流也没挂断"
            f"（已开流={opened.is_set()}, 已挂断={closed.is_set()}）"
        ) from None
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return status["code"], closed.is_set()



async def test_stream_delivers_a_frame_to_a_browser(app, client, box: Box, jpeg: bytes):
    """⭐ 正路：盒子推上来的字节，原样出现在浏览器的 MJPEG 流里。

    这条是"能在网页上看到现场画面"这件事的全部证据 ——
    它跨过了鉴权、路由、hub、multipart 封装和响应头五段代码，
    任何一段断了这里都会红。
    """
    app.state.frame_hub.publish(box.device_id, jpeg)
    ticket = client.post(
        f"/api/devices/{box.device_id}/stream-ticket", headers=box.auth
    ).json()["ticket"]

    code, headers, body = await _read_one_frame(
        app, f"/api/devices/{box.device_id}/stream", {"ticket": ticket}, jpeg
    )

    assert code == 200
    assert headers[b"content-type"] == b"multipart/x-mixed-replace; boundary=ipframe"
    # 票据在 URL 里，别让它随 Referer 漏给第三方站点
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert b"no-store" in headers[b"cache-control"]

    # 分帧格式必须是浏览器认的那种：边界 + 类型 + 长度 + 空行 + 字节
    assert body.startswith(b"--ipframe\r\nContent-Type: image/jpeg\r\n")
    assert b"Content-Length: " + str(len(jpeg)).encode() in body
    head, _, rest = body.partition(b"\r\n\r\n")
    assert rest.startswith(jpeg), "帧被改过了 —— 中心在这条路上不该动任何字节"


async def test_stream_skips_an_old_frame_instead_of_replaying_it(
    app, client, box: Box, jpeg: bytes
):
    """观众慢了一拍时，看到的是**现在**，不是攒下来的过去。

    先发 f1、再发 f2，然后才开流：读者应该只拿到 f2。
    若哪天有人在 hub 里加队列"别丢帧"，这条会红 ——
    而排队正是最该避免的：落后的直播比丢帧糟糕得多。
    """
    hub = app.state.frame_hub
    hub.publish(box.device_id, b"stale-frame")
    hub.publish(box.device_id, jpeg)

    ticket = client.post(
        f"/api/devices/{box.device_id}/stream-ticket", headers=box.auth
    ).json()["ticket"]
    _, _, body = await _read_one_frame(
        app, f"/api/devices/{box.device_id}/stream", {"ticket": ticket}, jpeg
    )

    assert b"stale-frame" not in body
    assert body.count(b"--ipframe\r\n") == 1, "一帧就够停下，不该攒在后面"


# ---------------------------------------------------------------- 盒子自报诊断
#
# 这一组守住的是**网页上那块调试面板的可信度**。它显示的每一个数字都来自
# 盒子自己，中心只是转存；于是最危险的失败不是报错，而是**静默地显示错的值**：
# 把"盒子没报过"显示成 0 帧会让人去查摄像头，把一条旧错误的时间顶到"刚刚"
# 会让人以为它正在发生。下面每条都对着一种"看起来正常、其实在骗人"。
def test_status_report_is_persisted_and_reachable_from_the_api(client, box: Box):
    """盒子报上来的状态要真的进库，且从设备接口上读得到。

    端到端走一遍（WS → DB → REST）是因为这条链路上有两次"字段漏了"的机会：
    `schemas.DeviceOut` 少写一个字段，盒子照样报、库里照样存，只是网页永远
    看不到 —— 而网页是唯一有人看的那一端。
    """
    with _connected(client, box) as ws:
        _auth(ws, box.token)
        ws.send_json(
            {
                "type": "status",
                "version": "1.4.0",
                "streaming": True,
                "stream_enabled": True,
                "live_detection": False,
                "frames_sent": 128,
                "uploads": 7,
                "pending_uploads": 2,
                "last_error": "推流取帧失败：CameraError: 打不开 /dev/video0",
                "last_error_at": "2026-11-01T10:00:00+00:00",
            }
        )
        _barrier(ws)

        row = _device_row(client.app, box.device_id)
        assert row.agent_streaming is True
        assert row.agent_stream_enabled is True
        assert row.agent_live_detection is False
        assert row.agent_frames_sent == 128
        assert row.agent_uploads == 7
        assert row.agent_pending_uploads == 2
        assert row.agent_last_error.startswith("推流取帧失败")
        assert row.agent_version == "1.4.0"
        assert row.agent_status_at is not None

        body = client.get(f"/api/devices/{box.device_id}", headers=box.auth).json()
        assert body["agent_frames_sent"] == 128
        assert body["agent_streaming"] is True
        assert body["agent_last_error_at"] is not None


def test_a_box_that_never_reported_shows_none_not_zero(client, box: Box):
    """⭐ 从没报过 status 的盒子，所有诊断字段必须是 `null`，**不能是 0 / false**。

    "推了 0 帧"和"不知道推了几帧"是两件完全不同的事：前者指向摄像头坏了，
    后者只是这台盒子跑的旧版本、不发 status 报文。把后者显示成前者，
    会让看面板的人去拆一台好盒子。这是这块面板最可能骗人的一种方式。
    """
    body = client.get(f"/api/devices/{box.device_id}", headers=box.auth).json()
    for key in (
        "agent_streaming",
        "agent_stream_enabled",
        "agent_live_detection",
        "agent_frames_sent",
        "agent_uploads",
        "agent_pending_uploads",
        "agent_last_error",
        "agent_last_error_at",
        "agent_status_at",
    ):
        assert body[key] is None, f"{key} 在盒子没上报时必须是 null"


def test_dirty_status_fields_are_ignored_without_breaking_the_stream(
    client, box: Box, jpeg: bytes
):
    """⭐ 字段类型全错的 status 报文：丢掉那些字段，**但画面照旧**。

    链路另一头是可能跑着旧版本、也可能被人改过的树莓派。一条诊断报文格式不对
    就把推流连接掐断，是最坏的那种失败 —— 真正值钱的画面是好的，却连着一起没了。
    所以这里断言的是"推流还在继续"，而不是"没报错"。
    """
    with _connected(client, box) as ws:
        _auth(ws, box.token)
        ws.send_json(
            {
                "type": "status",
                "streaming": 1,              # 不是 bool
                "frames_sent": "128",        # 不是数字
                "uploads": 3.7,              # 带小数的浮点：宁可丢掉，也不许中心悄悄取整
                "last_error": 123,           # 不是字符串
                "last_error_at": "not-a-time",
            }
        )
        _barrier(ws)

        row = _device_row(client.app, box.device_id)
        # bool 与 int 不许互相串味：`isinstance(True, int)` 在 Python 里成立，
        # 放行的话 `{"streaming": 1}` 会被两边同时接受。
        assert row.agent_streaming is None
        assert row.agent_frames_sent is None
        assert row.agent_last_error is None
        assert row.agent_last_error_at is None

        # 连接还活着、画面还推得动 —— 这才是这条用例真正要守的东西。
        # （不断言 `last_frame_at`：心跳落库是按 heartbeat_s 节流的，
        #   上面那条 ping 刚写过，这一帧很可能被节流挡掉，那是预期的。）
        ws.send_bytes(jpeg)
        _barrier(ws)
        assert client.app.state.frame_hub.latest(box.device_id) is not None


def test_status_keeps_a_non_streaming_box_online(client, box: Box):
    """不推流的盒子（`stream_enabled=false`）靠 status 报文维持在线。

    它每 30 分钟才上报一次结果，而 `online` 的窗口只有 60 秒 —— 没有 status
    报文的话，一台**正在正常工作**的盒子会被永久显示成离线。
    """
    row = _device_row(client.app, box.device_id)
    session = client.app.state.session_factory()
    try:
        device = session.get(Device, row.id)
        device.last_seen_at = None
        session.commit()
    finally:
        session.close()
    assert client.get(f"/api/devices/{box.device_id}", headers=box.auth).json()["online"] is False

    with _connected(client, box) as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "status", "streaming": False, "stream_enabled": False})
        _barrier(ws)
        assert client.get(f"/api/devices/{box.device_id}", headers=box.auth).json()["online"] is True


def test_ping_refreshes_presence_too(client, box: Box):
    """`ping` 也要刷新心跳 —— 老盒子的在线凭据就是它。

    中心的 `ping` 分支原本只回 pong、不碰 `last_seen_at`。而盒子的
    `_pump_heartbeat` 恰好只发 ping：两边一凑，就是把一台正常工作的
    不推流盒子显示成离线。这条把"ping 也算听到它了"钉住。
    """
    session = client.app.state.session_factory()
    try:
        device = session.get(Device, box.device_id)
        device.last_seen_at = None
        session.commit()
    finally:
        session.close()

    with _connected(client, box) as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        _barrier(ws)
        assert client.get(f"/api/devices/{box.device_id}", headers=box.auth).json()["online"] is True


def _stretch_throttle_window(app, seconds: float = 3600.0) -> None:
    """把心跳落库的节流窗口拉长（配置是 frozen dataclass，只能整体替换）。

    拉长之后"写没写库"就只由**内容**决定，不再掺进"上次写是什么时候" ——
    用例要证的是"翻转该不该写"，不该被真实时钟搅浑。
    """
    current = app.state.settings
    app.state.settings = replace(
        current, agent=replace(current.agent, heartbeat_s=seconds)
    )


def test_a_flipped_switch_is_written_even_inside_the_throttle_window(client, box: Box):
    """⭐ 状态翻转必须**立刻**落库，不能被心跳节流挡掉。

    节流是为了保护 SD 卡，但"刚报上来的摄像头错误被节流挡掉"意味着面板
    在真正出问题的时间窗里显示一切正常 —— 那这块面板就白做了。
    所以开关翻转、版本变化、出现新错误，都无视节流窗口直接写。
    """
    _stretch_throttle_window(client.app)

    with _connected(client, box) as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "status", "streaming": True, "frames_sent": 10})
        _barrier(ws)
        assert _device_row(client.app, box.device_id).agent_streaming is True

        # 仍在节流窗口内，且帧数变化不算"翻转" —— 这一条**应当**被挡掉
        ws.send_json({"type": "status", "streaming": True, "frames_sent": 99})
        _barrier(ws)
        assert _device_row(client.app, box.device_id).agent_frames_sent == 10

        # 开关翻了：不等窗口，立刻写
        ws.send_json({"type": "status", "streaming": False, "frames_sent": 99})
        _barrier(ws)
        row = _device_row(client.app, box.device_id)
        assert row.agent_streaming is False
        assert row.agent_frames_sent == 99


def test_a_partial_status_does_not_erase_what_the_box_already_told_us(client, box: Box):
    """盒子少报一个字段（多半是版本较旧）不该把既有值抹成 NULL。

    旧盒子报上来的 128 帧，不能因为新盒子多发了一个字段、旧盒子没发
    就被写成"未知"。None 在这里的语义是"这一项没报"，不是"这一项是空"。
    """
    _stretch_throttle_window(client.app)

    with _connected(client, box) as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "status", "frames_sent": 128, "streaming": True})
        _barrier(ws)
        # 只带一个 type 的"空报文"：靠"有新错误/翻转"仍会写库，但值一个都不能丢
        ws.send_json({"type": "status", "streaming": False})
        _barrier(ws)
        row = _device_row(client.app, box.device_id)
        assert row.agent_frames_sent == 128, "没报的字段不该被清空"
        assert row.agent_streaming is False, "报了且翻转了的字段必须更新"


def test_a_repeated_error_does_not_refresh_its_timestamp(client, box: Box):
    """同一条错误被反复上报时，时刻**不许**跟着往前跑。

    否则一条三天前的瞬时错误，会因为盒子一直在报它而永远显示"刚刚发生" ——
    看面板的人会以为故障正在进行。时刻只在**错误内容变了**时刷新。
    """
    _stretch_throttle_window(client.app)

    with _connected(client, box) as ws:
        _auth(ws, box.token)
        ws.send_json({"type": "status", "last_error": "摄像头打不开"})
        _barrier(ws)
        first_at = _device_row(client.app, box.device_id).agent_last_error_at

        ws.send_json({"type": "status", "last_error": "摄像头打不开"})
        _barrier(ws)
        assert _device_row(client.app, box.device_id).agent_last_error_at == first_at

        ws.send_json({"type": "status", "last_error": "上报被中心拒绝：401"})
        _barrier(ws)
        row = _device_row(client.app, box.device_id)
        assert row.agent_last_error == "上报被中心拒绝：401"
        assert row.agent_last_error_at != first_at
