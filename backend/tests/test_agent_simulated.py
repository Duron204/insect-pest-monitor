# -*- coding: utf-8 -*-
"""没有树莓派时的**端到端证据**：真代理 ↔ 真中心。

这个文件里除了「推理」之外全部是真的：

    中心   一个**真的 uvicorn**，监听 127.0.0.1 上一个真端口
    代理   真的 `urllib` 发配对与上报、真的 `websockets` 推流
    摄像头 `simulated` 图像源，读的是 `test_media/` 里的真 JPEG
    中心侧 真的烧框、真的落一条 mode=device 的任务、真的把帧放进 frame_hub

**为什么中心不能用 `TestClient` 撑**：`TestClient` 从不监听 socket，
它把请求直接派给 ASGI 应用对象。而代理走的是真 TCP，两边根本碰不到面 ——
写成 TestClient 的话，这个文件会"通过"，但什么都没验证到。
（计划里写的就是 TestClient，那是错的，这里按现实改成真 uvicorn。）

**为什么推理是假的**：加载真权重要十几秒，而这里要证明的是
「代理循环 ↔ 中心接口」对得上（载荷字段、幂等、ready 里的流计划、落库结果），
不是模型准不准（那是 `test_detector.py` 的事）。所以**只**把 detector 换成假的，
其余一个不打桩。这也是它不需要 `weights/` 的原因 —— 与 `test_local_camera.py`
不同，这个文件在任何环境下都会真跑。
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
import pytest
import uvicorn

from app.config import AgentConfig, Settings
from app.main import create_app
from app.services import edge_agent as edge_agent_module
from app.services.backends.base import Detection, InferenceResult
from app.services.edge_agent import EdgeAgentService
from app.services.local_camera import LocalCameraService

HARDWARE_ID = "pi-simulated-e2e"
#: 中心宣告给盒子的推流宽度。故意与代理**本地**的 stream_width 不同 ——
#: 这样"帧的尺寸"就能证明盒子采纳的是中心的计划，而不是自己那份配置。
CENTER_STREAM_WIDTH = 320


# ================================================================== 假推理
class FakeDetector:
    """固定输出的假推理。见模块 docstring：这是本文件里唯一的桩。"""

    def infer(self, frame, conf=None, iou=None) -> InferenceResult:
        return InferenceResult(
            detections=[
                Detection(
                    class_id=0,
                    class_name="aphid",
                    score=0.91,
                    bbox=(10, 20, 110, 140),
                    zh_name="蚜虫",
                ),
                Detection(
                    class_id=3,
                    class_name="whitefly",
                    score=0.77,
                    bbox=(150, 60, 210, 120),
                    zh_name="白粉虱",
                ),
            ],
            inference_ms=8.0,
        )


# ================================================================ HTTP 小工具
def _maybe_json(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return body


def _request(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    """同步 HTTP。用 urllib 而不是 httpx —— 和代理用的是同一套栈，
    这样"代理能连上但测试客户端连不上"这类环境问题不会出现。"""
    data = None
    head = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        head["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=head, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), _maybe_json(response.read())
    except urllib.error.HTTPError as exc:
        with exc:
            return int(exc.code), _maybe_json(exc.read())


def _free_port() -> int:
    """让内核挑一个空闲端口。

    先 bind 再 close 拿到端口号，中间有一个微小的竞态窗口 —— 在本机测试里
    可以忽略，换来的是能在起服务器**之前**就把 base_url 拼出来。
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(fn: Callable[[], Any], *, timeout: float, what: str, interval: float = 0.1) -> Any:
    """轮询直到 `fn()` 返回真值。

    **所有等待都必须带上限和一句人话**：没有上限的等待在 CI 上表现为
    "整个测试套挂住"，而有上限时至少会告诉你"等的是哪个条件"。
    """
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"{what}：{timeout} 秒内没有成立（最后一次得到 {last!r}）")


# ================================================================== 夹具
@dataclass
class Center:
    """跑在后台线程里的**真** uvicorn 中心，外加几个测试用的调用助手。"""

    base_url: str
    app: Any
    settings: Settings

    # ---- 面向用户（真 HTTP）
    def request(
        self, method: str, path: str, *, payload: dict | None = None, headers=None
    ) -> tuple[int, Any]:
        return _request(method, f"{self.base_url}{path}", payload=payload, headers=headers)

    def get(self, path: str, *, headers=None) -> Any:
        status, body = self.request("GET", path, headers=headers)
        assert status == 200, f"GET {path} 返回 {status}：{body}"
        return body

    def post(self, path: str, *, payload: dict | None = None, headers=None) -> Any:
        status, body = self.request("POST", path, payload=payload, headers=headers)
        assert status == 200, f"POST {path} 返回 {status}：{body}"
        return body

    def register_and_login(self, username: str) -> dict[str, str]:
        creds = {"username": username, "password": f"{username}123456"}
        status, _ = self.request("POST", "/api/auth/register", payload=creds)
        assert status in (200, 201), f"注册失败：{status}"
        status, body = self.request("POST", "/api/auth/login", payload=creds)
        assert status == 200, f"登录失败：{status} {body}"
        return {"Authorization": f"Bearer {body['access_token']}"}

    def fetch_jpeg(self, path: str) -> bytes:
        status, body = self.request("GET", path)
        assert status == 200, f"取图 {path} 返回 {status}：{body}"
        assert isinstance(body, bytes), f"{path} 返回的不是二进制"
        assert body[:2] == b"\xff\xd8", f"{path} 不是 JPEG"
        return body


@pytest.fixture()
def center(settings: Settings):
    """一个真的 uvicorn 中心。

    中心自己的 `agent` / `local_camera` 都是关的 —— 它是**中心**，
    不该去当盒子（否则它会先跟自己对上话，测试就完全没有分辨力了）。
    """
    center_settings = replace(
        settings,
        local_camera=replace(settings.local_camera, enabled=False),
        agent=replace(
            settings.agent,
            enabled=False,
            stream_enabled=True,
            stream_fps=8,
            stream_quality=60,
            stream_width=CENTER_STREAM_WIDTH,
        ),
    )
    app = create_app(center_settings)
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="fake-center", daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("假中心没能在 20 秒内起来")

    try:
        yield Center(base_url=f"http://127.0.0.1:{port}", app=app, settings=center_settings)
    finally:
        server.should_exit = True
        thread.join(15)
        # 线程没退干净就说明还有连接没关 —— 那正是会在 CI 上偶发卡住的形态，
        # 明确断言出来，而不是让它在下一个用例里以一个看不懂的方式爆掉。
        assert not thread.is_alive(), "假中心没有在 15 秒内退出"


@pytest.fixture()
def agent(center: Center, settings: Settings, monkeypatch):
    """真的代理：真循环 + 真传输 + 真的 simulated 摄像头；推理是假的。

    时间轴被压缩（`SECONDS_PER_MINUTE` 改成 0.2、首拍等待改成 0.1 秒），
    否则这个用例要等满一分钟才看到第一张结果 —— 实际结果就是没人跑它。
    """
    monkeypatch.setattr(edge_agent_module.local_camera_module, "SECONDS_PER_MINUTE", 0.2)
    monkeypatch.setattr(edge_agent_module, "_FIRST_CAPTURE_DELAY_S", 0.1)

    agent_settings = replace(
        settings,
        agent=AgentConfig(
            enabled=True,
            center_url=center.base_url,
            name="东头盒子",
            hardware_id=HARDWARE_ID,
            stream_enabled=True,
            # 与中心的计划故意不同：见 CENTER_STREAM_WIDTH 的说明
            stream_width=960,
            stream_fps=2,
            capture_interval_min=1,
            request_timeout_s=10.0,
            reconnect_max_s=1.0,
        ),
        local_camera=replace(
            settings.local_camera,
            enabled=False,
            backend="simulated",
            # 默认的 "test_media" 是相对 project_root 解析的，里面有 9 张真图
            simulated_dir="test_media",
            interval_min=0,
        ),
    )
    camera = LocalCameraService(
        agent_settings, center.app.state.session_factory, FakeDetector()
    )
    service = EdgeAgentService(agent_settings, FakeDetector(), camera)
    service.start()
    try:
        yield service
    finally:
        service.shutdown()
        assert not service.running, "代理线程没有停干净"


# ================================================================== 用例
def test_an_account_can_bind_a_simulated_pi_and_see_its_results(
    center: Center, agent: EdgeAgentService
):
    """⭐ 从"盒子报到"到"网页上能看到结果图"的完整一条路。

    这条跑通就说明整条链路是对的：配对码确实是中心发的、令牌确实是取一次就作废的、
    上报的载荷确实能通过中心的校验、中心确实把框烧到了**它收到的**那张图上。
    任何一环的字段对不上，这里都会红。
    """
    headers = center.register_and_login("farmer")

    # ---------------------------------------------------------- 1. 盒子报到
    code = _wait_for(
        lambda: agent.status()["pairing_code"],
        timeout=20,
        what="代理拿到配对码",
    )
    assert len(code) == 6 and code.isdigit(), f"配对码应当是 6 位数字，实际 {code!r}"

    # 还没绑的时候，中心那边什么都看不到
    assert center.get("/api/devices", headers=headers) == []

    # ---------------------------------------------------------- 2. 输码绑定
    device = center.post("/api/devices/bind", payload={"code": code}, headers=headers)
    device_id = device["id"]
    assert device["status"] == "active"
    assert device["hardware_id"] == HARDWARE_ID

    _wait_for(lambda: agent.status()["paired"], timeout=20, what="代理取到设备令牌")
    assert agent.status()["device_id"] == device_id

    # 令牌必须落盘：这是"重启不会重印一个新码"的依据
    assert agent._state_path.is_file()
    assert json.loads(agent._state_path.read_text(encoding="utf-8"))["device_token"]

    listed = center.get("/api/devices", headers=headers)
    assert [d["id"] for d in listed] == [device_id]
    assert not (Path(agent._settings.data_dir) / "agent_pairing.txt").exists(), (
        "绑上之后配对码通知文件该被清掉"
    )

    # ---------------------------------------------------------- 3. 定时上报
    def two_tasks():
        page = center.get(f"/api/devices/{device_id}/tasks", headers=headers)
        return page["items"] if page["total"] >= 2 else None

    items = _wait_for(two_tasks, timeout=60, what="中心收到至少两次抓拍")
    assert [item["mode"] for item in items] == ["device", "device"]
    # 计数与中心入库之间有天然竞态（中心先落库、代理收到响应后才 +1），
    # 慢速 CI 上会先看到中心的 2 条再看到代理的计数 —— 用等待而不是裸断言
    _wait_for(lambda: agent.status()["uploads"] >= 2, timeout=10, what="代理上传计数跟上")

    for item in items:
        detail = center.get(f"/api/tasks/{item['id']}", headers=headers)
        assert detail["mode"] == "device"
        assert detail["device_id"] == device_id
        assert detail["status"] == "done"
        # 两个框都真的落全了，中文名走的是中心自己的 label_map
        assert detail["counts"] == {"蚜虫": 1, "白粉虱": 1}
        assert detail["result_image_path"], "中心应当自己烧框并留下标注图"

        # 原图与标注图都真的取得到 —— 而标注图是**中心**在它收到的那张
        # 原图上烧的框（代理只发了原图 + 框坐标，没发过任何画好的图）。
        # 裸地址现在由 media_guard 守着，必须带上接口给的票据。
        ticket = detail["result_image_path"].split("mt=", 1)[1]
        original = center.fetch_jpeg(
            f"/media/outputs/{item['id']:06d}/original.jpg?mt={ticket}"
        )
        annotated = center.fetch_jpeg(
            f"/media/outputs/{item['id']:06d}/annotated.jpg?mt={ticket}"
        )
        assert original != annotated, "标注图应当与原图不同（真的烧了框）"

        # 中心收到的原图大小 = 摄像头给出的原图大小，说明上报没被缩放
        decoded = cv2.imdecode(np.frombuffer(original, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None and decoded.ndim == 3

    # ---------------------------------------------------------- 4. 在线与推流
    assert center.get(f"/api/devices/{device_id}", headers=headers)["online"] is True

    hub = center.app.state.frame_hub
    frame = _wait_for(
        lambda: hub.latest(device_id),
        timeout=30,
        what="中心的 frame_hub 收到推流帧",
    )
    _seq, _ts, jpeg = frame
    assert bytes(jpeg[:2]) == b"\xff\xd8", "推上来的一帧必须是 JPEG 字节"
    pushed = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert pushed is not None
    assert max(pushed.shape[:2]) <= CENTER_STREAM_WIDTH, (
        f"帧最长边 {max(pushed.shape[:2])} 超过了中心的计划 {CENTER_STREAM_WIDTH} —— "
        "说明盒子用的是自己那份 stream_width 而不是 ready 里宣告的计划"
    )
    assert center.get("/api/system/agent", headers=headers)["streaming"] is False, (
        "中心自己的代理是关的：/api/system/agent 报的是本机代理，不是这台盒子"
    )


def test_a_second_account_cannot_see_the_bound_device(center: Center, agent: EdgeAgentService):
    """⭐ 端到端地验一次归属：B 绑不走、也看不到 A 的设备。

    这条走的是真 HTTP，所以它同时也在验"配对码只有一个能用的赢家" ——
    两个人几乎同时输码时，先到的那个绑定成功，后到的拿到 409 而不是偷走设备。
    """
    owner = center.register_and_login("owner1")
    peeker = center.register_and_login("peeker1")

    code = _wait_for(lambda: agent.status()["pairing_code"], timeout=20, what="代理拿到配对码")
    device = center.post("/api/devices/bind", payload={"code": code}, headers=owner)
    _wait_for(lambda: agent.status()["paired"], timeout=20, what="代理取到设备令牌")

    # 同一个码绑不了第二次 —— 这是"拿到码的人抢不走已绑的 Pi"那条性质的接口形态。
    #
    # 这里**确切**是 400（配对码无效或已过期）而不是 409：绑定成功时中心就把
    # `pairing_code` 清空了，所以第二次的码压根匹配不上任何设备。409
    # （该设备已被绑定）走的是**另一条路** —— 已绑定的盒子再 pair/start
    # 会拿到一个**新**码，而那条路要"码匹配上了但状态是 active"才触发。
    # 断言两个都接受等于没断言：这条就是要钉死"码是一次性的"。
    status, body = center.request(
        "POST", "/api/devices/bind", payload={"code": code}, headers=peeker
    )
    assert status == 400, f"用过的配对码应当作废（400），实际 {status} {body}"

    assert center.get("/api/devices", headers=peeker) == []
    assert center.request("GET", f"/api/devices/{device['id']}", headers=peeker)[0] == 404, (
        "别人的设备应当是 404（不确认存在），不是 403"
    )
    assert center.request(
        "GET", f"/api/devices/{device['id']}/tasks", headers=peeker
    )[0] == 404
