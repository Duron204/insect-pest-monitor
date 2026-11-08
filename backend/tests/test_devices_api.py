# -*- coding: utf-8 -*-
"""绑定设备：配对、绑定、解绑、归属隔离。

这一整个文件**不需要权重、不需要 torch** —— 阶段 2 里中心端还没有任何推理，
只有身份与配对。所以它跑得很快，每次改动后都应该跑。
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import Device, DeviceStatus, utcnow
from app.routers import devices as devices_module
from app.security import hash_device_token

HARDWARE_ID = "pi-serial-0001"


# ---------------------------------------------------------------- 夹具
# 限流清零（`_reset_device_rate_limiters`）与注册助手（`register_user`）
# 三个设备测试文件共用，见 conftest.py。
@pytest.fixture()
def user_a(register_user) -> dict[str, str]:
    return register_user("farmer")


@pytest.fixture()
def user_b(register_user) -> dict[str, str]:
    return register_user("intruder")


def _pair(client: TestClient, hardware_id: str = HARDWARE_ID, **extra) -> dict:
    """盒子发起配对。

    响应里有两样东西，**别只拿 code**：`code` 是给人抄到网页上的 6 位数字，
    `pairing_secret` 是给盒子自己轮询用的凭证。少了后者，中心一律回 `unknown`。
    """
    response = client.post(
        "/api/devices/pair/start", json={"hardware_id": hardware_id, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bind(client: TestClient, code: str, headers: dict[str, str]):
    return client.post("/api/devices/bind", json={"code": code}, headers=headers)


def _poll(client: TestClient, secret: str | None, hardware_id: str = HARDWARE_ID) -> dict:
    """盒子轮询配对结果。

    `secret` 传 `None` 表示**一个凭证都不带** —— 专门用来演示"只凭 hardware_id
    能得到什么"（答案是：什么也得不到）。要正常轮询就传 `_pair(...)["pairing_secret"]`。
    """
    headers = {} if secret is None else {"X-Pairing-Secret": secret}
    return client.get(
        "/api/devices/pair/poll", params={"hardware_id": hardware_id}, headers=headers
    ).json()


def _device_row(app, device_id: int) -> Device:
    session = app.state.session_factory()
    try:
        row = session.get(Device, device_id)
        session.expunge(row)
        return row
    finally:
        session.close()


# ---------------------------------------------------------------- 配对
def test_pair_start_returns_six_digit_code(client: TestClient):
    body = _pair(client, name="田里的盒子", model="Raspberry Pi 5 Model B")
    assert len(body["code"]) == 6 and body["code"].isdigit()
    assert body["expires_in"] == 300
    assert body["poll_interval_s"] == 2
    assert body["pairing_id"] > 0


def test_pair_poll_is_pending_before_bind(client: TestClient):
    pairing = _pair(client)
    body = _poll(client, pairing["pairing_secret"])
    assert body["status"] == "pending"
    assert body["device_token"] is None


def test_pair_start_hands_out_a_secret_next_to_the_code(client: TestClient):
    """配对码是给人抄的（所以只有 6 位），凭证是给盒子用的（所以可以足够长）。

    这条把两者的分工钉住：`code` 必须短到能抄（长度 6、纯数字），
    `pairing_secret` 必须长到猜不出来 —— 它是"只凭 hardware_id 能不能轮询"的答案。
    """
    body = _pair(client)
    assert len(body["pairing_secret"]) >= 32
    assert not body["pairing_secret"].isdigit()


def test_pair_poll_unknown_hardware(client: TestClient):
    assert _poll(client, "irrelevant", "never-seen")["status"] == "unknown"


def test_repairing_same_hardware_supersedes_code_and_keeps_one_row(
    client: TestClient, app, register_user
):
    """同一台盒子重新配对：只留一个活码，且**不新建行**。

    不按 hardware_id 复用行的话，盒子每次重启都会在库里留一具僵尸设备，
    用户的设备列表会越来越长。
    """
    first = _pair(client)
    second = _pair(client)
    assert first["pairing_id"] == second["pairing_id"]
    assert first["code"] != second["code"]

    session = app.state.session_factory()
    try:
        rows = session.query(Device).filter(Device.hardware_id == HARDWARE_ID).all()
        assert len(rows) == 1
        assert rows[0].pairing_code == second["code"]
    finally:
        session.close()

    # 旧码已作废
    assert _bind(client, first["code"], register_user("late")).status_code == 400


# ---------------------------------------------------------------- 绑定
def test_bind_and_list_own_device(client: TestClient, user_a):
    code = _pair(client)["code"]
    response = _bind(client, code, user_a)
    assert response.status_code == 200, response.text
    device = response.json()
    assert device["status"] == DeviceStatus.ACTIVE
    assert device["bound_at"] is not None

    listed = client.get("/api/devices", headers=user_a).json()
    assert [d["id"] for d in listed] == [device["id"]]
    # 新绑定的设备没有任何心跳，必须老实说"离线、0 帧率"，不能编一个数
    assert listed[0]["online"] is False
    assert listed[0]["stream_fps"] == 0.0


def test_bind_is_isolated_per_account(client: TestClient, user_a, user_b):
    """别人绑的设备，我一条都看不到，也一个接口都碰不到。"""
    device = _bind(client, _pair(client)["code"], user_a).json()

    assert client.get("/api/devices", headers=user_b).json() == []
    # 不是我的 → 404，**不是 403**：403 等于承认这个 id 存在
    assert client.get(f"/api/devices/{device['id']}", headers=user_b).status_code == 404
    assert (
        client.patch(
            f"/api/devices/{device['id']}", json={"name": "抢来的"}, headers=user_b
        ).status_code
        == 404
    )
    assert (
        client.post(f"/api/devices/{device['id']}/unbind", headers=user_b).status_code == 404
    )
    assert client.get(f"/api/devices/{device['id']}/tasks", headers=user_b).status_code == 404
    # 而且改名真的没生效
    assert client.get(
        f"/api/devices/{device['id']}", headers=user_a
    ).json()["name"] == device["name"]


def test_bind_requires_auth(client: TestClient):
    code = _pair(client)["code"]
    assert _bind(client, code, {}).status_code == 401
    assert client.get("/api/devices").status_code == 401


def test_wrong_code_is_rejected(client: TestClient, user_a):
    real = _pair(client)["code"]
    wrong = "000000" if real != "000000" else "111111"
    assert _bind(client, wrong, user_a).status_code == 400


def test_expired_code_is_rejected_and_cleaned_up(client: TestClient, app, user_a):
    """过期码两件事：**绑不上**，而且盒子下一次轮询会知道"该重新配对了"。

    这里刻意把清理的归属写清楚：过期不靠"失败的绑定"顺手清，
    而是靠 `pair/poll` 的 expired 分支 —— 因为盒子是会一直轮询的那一方，
    清理必须挂在它一定会走到的路径上，否则一个没人尝试绑定的过期码会永远躺在库里。
    """
    pairing = _pair(client)
    code, secret = pairing["code"], pairing["pairing_secret"]
    device_id = _poll(client, secret)["device_id"]

    session = app.state.session_factory()
    try:
        session.get(Device, device_id).pairing_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    assert _bind(client, code, user_a).status_code == 400

    # 盒子继续轮询 → 收到 expired，而不是继续 pending 空等
    assert _poll(client, secret)["status"] == "expired"
    assert _device_row(app, device_id).pairing_code is None
    # 码清掉之后**仍然是 expired**（不是 unknown）：中心确实认识这台硬件，
    # 只是它手上没有有效的码。盒子对这两种状态的动作一样（重新 pair/start），
    # 但报 unknown 会误导排查 —— 那读起来像"中心不认识我这台盒子"。
    # 加 poll 一次，确认它能稳定停在 expired 而不来回抖。
    assert _poll(client, secret)["status"] == "expired"


def test_code_with_spaces_is_accepted(client: TestClient, user_a):
    """用户从终端抄码时很容易连同空格一起粘贴，不该因此失败。"""
    code = _pair(client)["code"]
    assert _bind(client, f"{code[:3]} {code[3:]}", user_a).status_code == 200


def test_binding_an_active_device_is_conflict_and_does_not_steal_it(
    client: TestClient, user_a, user_b
):
    """⭐ 本方案最要紧的一条安全性质：**拿到配对码 ≠ 能抢走别人的设备**。

    盒子被解绑前会一直重发配对码（它无从知道自己已被绑），所以 B 完全可能
    拿到一个真实存在的码。这时必须是 409，且设备必须**仍然属于 A**。
    """
    device = _bind(client, _pair(client)["code"], user_a).json()

    stolen_code = _pair(client)["code"]  # 同一台盒子再次配对，照样发码
    response = _bind(client, stolen_code, user_b)
    assert response.status_code == 409
    assert "已被绑定" in response.json()["detail"]

    # 再来一次仍然是 409（码没被消费掉），而不是退化成"码无效"
    assert _bind(client, stolen_code, user_b).status_code == 409
    assert [d["id"] for d in client.get("/api/devices", headers=user_a).json()] == [device["id"]]
    assert client.get("/api/devices", headers=user_b).json() == []


# ---------------------------------------------------------------- 令牌
def test_poll_hands_over_token_exactly_once(client: TestClient, app, user_a):
    pairing = _pair(client)
    device = _bind(client, pairing["code"], user_a).json()

    first = _poll(client, pairing["pairing_secret"])
    assert first["status"] == "paired"
    assert first["device_id"] == device["id"]
    token = first["device_token"]
    assert token

    # 中心端只存 hash，从不存明文；明文取走即清
    row = _device_row(app, device["id"])
    assert row.token_hash == hash_device_token(token)
    assert row.pending_token_plain is None

    # 第二次轮询再也拿不到令牌 —— 而且这次连凭证都已经作废了（交付即失效）
    second = _poll(client, pairing["pairing_secret"])
    assert second["status"] == "unknown"
    assert second["device_token"] is None


def test_the_token_cannot_be_claimed_with_hardware_id_alone(
    client: TestClient, app, user_a
):
    """⭐ 配对凭证要挡住的那件事：知道 `hardware_id` 的人在交接窗口里插队。

    `hardware_id` 是树莓派的 serial，明写在 `/proc/cpuinfo` 里 —— 对"谁知道"
    这个问题它等同于公开信息。而"绑定完成"到"真盒子把令牌取走"之间有一小段窗口
    （`pending_token_plain` 落库正是为了撑过盒子重启）。没有凭证的话，这段时间里
    任何知道那串 serial 的人都能赶在真盒子之前把设备令牌领走，
    之后就能以这台盒子的身份往账号里写数据、并占用它那条推流。

    这条用例还顺带钉住一件事：**失败不消耗令牌**。攻击者问两遍之后，
    真盒子必须还能一次拿到 —— 否则这个"防抢"就退化成"谁都能把交接搅黄"。
    """
    pairing = _pair(client)
    device = _bind(client, pairing["code"], user_a).json()

    # 攻击者知道 hardware_id，也可能看过/猜过配对码 —— 但他没有凭证
    without = _poll(client, None)
    guessed = _poll(client, "0" * 43)
    for attempt in (without, guessed):
        assert attempt["device_token"] is None
        # 而且连"这台硬件正在等配对"都确认不了：回答与不存在的硬件逐字相同
        assert attempt["status"] == "unknown"
    assert _device_row(app, device["id"]).pending_token_plain is not None

    # 真盒子带着凭证来，一次拿到
    real = _poll(client, pairing["pairing_secret"])
    assert real["status"] == "paired"
    assert real["device_id"] == device["id"]
    assert real["device_token"]


def test_a_foreign_pair_start_cannot_steal_the_pending_token(
    client: TestClient, app, user_a
):
    """⭐ 别人再发一次 `pair/start`，**不能顶替令牌的领取者**。

    `pair/start` 是匿名的，谁都能用别人的 hardware_id 调它，它会把配对码和
    "当前"凭证一起换成新的 —— 这一点挡不住（那是首次接触本身的属性，见
    devices.py 的模块头）。但"能换掉码"不等于"能换掉领令牌的资格"：
    令牌只交给**把这次配对做到底**的那枚凭证（`bind_device` 时快照下来的
    `pending_token_secret_hash`）。

    为什么非这样不可 —— 这正是上一版**没有**做到的地方，值得写清楚：
    只认"当前那一枚"的话，知道 serial 的人在 `bind` 成功之后、真盒子来取之前
    自己发一次 `pair/start`，就凭空拿到了唯一的领取凭证。真盒子下次轮询
    凭证不认 → 它会照设计清掉本地状态、重新配对；而就在那个来回里，
    令牌落到了发起这轮配对的人手上。这不是"搅黄了一次交接"，是**偷**。

    所以这条用例断言三件事，缺一不可：
      1. 换码之后明文令牌没被顺手清掉（清掉会让设备卡在 active 却拿不到令牌）；
      2. 真盒子**手里那枚**照样能取走令牌；
      3. 换码的人**永远取不到** —— 他只能看到 `unknown`。
    """
    pairing = _pair(client)
    device = _bind(client, pairing["code"], user_a).json()

    # 旁人（只知道 hardware_id）发起的配对：码和"当前"凭证都被顶掉了
    foreign = _pair(client)
    assert foreign["pairing_secret"] != pairing["pairing_secret"]
    assert _device_row(app, device["id"]).pending_token_plain is not None

    # 3. 换码的人拿不到令牌。他手里那枚确实是"当前"凭证，但快照不是它。
    thief = _poll(client, foreign["pairing_secret"])
    assert thief["status"] == "unknown"
    assert thief["device_token"] is None

    # 2. 真盒子手里那枚（绑定那一刻的快照）照样有效，一次拿到
    real = _poll(client, pairing["pairing_secret"])
    assert real["status"] == "paired"
    assert real["device_id"] == device["id"]
    assert real["device_token"]
    # 交接完成：快照与当前凭证一起作废，第二个人再来就什么都没有了
    assert _poll(client, foreign["pairing_secret"])["status"] == "unknown"
    assert _poll(client, pairing["pairing_secret"])["status"] == "unknown"


def test_unbind_also_clears_the_token_handover_snapshot(
    client: TestClient, app, user_a
):
    """解绑要把**两枚**凭证都清掉，只清一枚等于留了把备用钥匙。

    解绑清 `pairing_secret_hash` 是有意的（旧代理不该在下次 poll 里被认成
    合法配对者）。快照 `pending_token_secret_hash` 是同一类东西 —— 它也是
    "这次绑定"的凭证，只是绑在另一个时刻 —— 所以必须一起清。
    漏掉它的症状：一个揣着旧凭证的代理在解绑后仍能通过第一道检查，
    走到 `_hand_over_token` 才发现没令牌可取（幸运的话），
    或者在新主人绑定成功之后**把本该交给新盒子的令牌取走**（不幸的话）。
    """
    pairing = _pair(client)
    device = _bind(client, pairing["code"], user_a).json()
    assert client.post(
        f"/api/devices/{device['id']}/unbind", headers=user_a
    ).status_code == 200

    row = _device_row(app, device["id"])
    assert row.pairing_secret_hash is None
    assert row.pending_token_secret_hash is None, "解绑后不该再有人能领走令牌"
    assert row.pending_token_plain is None
    assert _poll(client, pairing["pairing_secret"])["status"] == "unknown"


# ---------------------------------------------------------------- 限流
def test_repeated_wrong_codes_are_throttled(client: TestClient, user_a):
    real = _pair(client)["code"]
    wrongs = [f"{i:06d}" for i in range(1, 13) if f"{i:06d}" != real][:10]

    for wrong in wrongs:
        assert _bind(client, wrong, user_a).status_code == 400

    # 窗口内已经用满 10 次 → 第 11 次直接 429
    assert _bind(client, wrongs[0], user_a).status_code == 429


def test_wrong_guesses_do_not_disturb_the_live_code(client: TestClient, app, user_a, user_b):
    """别人乱猜**不能**把一次正常的待配对搅黄。

    这条是可证伪的反向护栏：曾经有个"同一个码失败 5 次就作废"的计数器，
    它的注释说在防爆破，实际上一失败的猜测按定义就没匹配上任何设备，
    那个查询永远查不到东西。这个用例把"猜错码不产生副作用"钉住 ——
    将来谁再往失败路径上加重逻辑（清码、锁设备），这里立刻会红。
    """
    pairing = _pair(client)
    code = pairing["code"]
    device_id = _poll(client, pairing["pairing_secret"])["device_id"]

    for i in range(9):  # 9 次，正好压在 B 的 10 次额度之下
        wrong = f"{i:06d}"
        if wrong == code:
            continue
        assert _bind(client, wrong, user_b).status_code == 400

    # 码原封不动，A 用真码照样绑得上
    assert _device_row(app, device_id).pairing_code == code
    assert _bind(client, code, user_a).status_code == 200


def test_repeated_bad_pairing_secrets_do_not_flood_the_log(
    client: TestClient, caplog
):
    """凭证不匹配要**留痕**，但按硬件每分钟只留一条。

    为什么单独给"日志"加一道闸门，而不是给请求限流：这条接口的**正常**流量
    就是每 2 秒一次（盒子在轮询），给它限流会把正常配对一起打死。
    所以要压的是日志，不是请求 —— 而日志确实会被压爆：凭证不对的分支
    在正常轮询里永远走不到，一旦出现就是"有人在猜"或"盒子与中心版本不一致"，
    两种情况都得有人看见；但拿一个已知 serial 按轮询频率刷，每秒一行会把
    真正想提示的人埋掉。

    这里刷 30 次（远超每分钟一条的额度），断言只留下 1 条 —— 而且
    **不能断言"一条都没有"**：那是监控瞎掉，比刷屏更糟。
    """
    _pair(client)
    with caplog.at_level(logging.WARNING, logger="insect"):
        for _ in range(30):
            assert _poll(client, "0" * 43)["status"] == "unknown"

    warned = [r for r in caplog.records if "凭证不匹配" in r.getMessage()]
    assert len(warned) == 1, f"应折叠成 1 条，实际 {len(warned)} 条"


# ---------------------------------------------------------------- 匿名接口的边界
# `pair/start` 是匿名的，而它既能**往别人的行上写**、又能**建新的行**。
# 下面这几条钉住这两个方向上的边界。
def test_a_foreign_pair_start_cannot_rename_my_device(client: TestClient, app, user_a):
    """别人再发一次 `pair/start`，不能改掉我设备的名字。

    盒子自报的 name/model/agent_version/backend 都是**写操作**，而这条接口匿名、
    认人只看 `hardware_id`（那是 /proc/cpuinfo 里的 serial，不是秘密）。
    所以自报字段只允许写进"还没有主人"的行 —— 否则任何知道 serial 的人
    都能把别人田里那台盒子的名字改掉，而主人只会看到自己的设备莫名改名。
    """
    device = _bind(client, _pair(client, name="东头棚的盒子")["code"], user_a).json()
    assert device["name"] == "东头棚的盒子"

    _pair(client, name="已经被我改掉了", model="攻击者的型号", agent_version="9.9.9")

    row = _device_row(app, device["id"])
    assert row.name == "东头棚的盒子"
    assert row.model != "攻击者的型号"
    assert row.agent_version != "9.9.9"

    # 反向护栏：这道门不能把**正常**的重新配对夹住。解绑之后设备无主，
    # 盒子重新报到时自报的字段必须照收 —— 否则修完这个洞，真实用例也一起废了。
    client.post(f"/api/devices/{device['id']}/unbind", headers=user_a)
    _pair(client, name="重新报到", model="Raspberry Pi 5 Model B")
    row = _device_row(app, device["id"])
    assert row.name == "重新报到"
    assert row.model == "Raspberry Pi 5 Model B"


def test_pair_start_stops_creating_rows_at_the_cap(client: TestClient, monkeypatch):
    """设备表有上限：这条匿名接口不能被用来无限建行。

    表只增不减（解绑刻意不删行，历史要靠它），没有上限时
    "能连到中心"就等于"能往 SD 卡里写任意多行"。
    上限用一个很小的数来测，免得用例真去建 2000 行。
    """
    monkeypatch.setattr(devices_module, "MAX_DEVICES", 2)
    _pair(client, hardware_id="pi-1")
    _pair(client, hardware_id="pi-2")

    third = client.post("/api/devices/pair/start", json={"hardware_id": "pi-3"})
    assert third.status_code == 503
    assert "上限" in third.json()["detail"]

    # 已经认识的那两台照常发码 —— 顶住的只是"建新行"，不是整个接口
    assert _pair(client, hardware_id="pi-1")["code"]


def test_the_cap_reclaims_stale_pending_rows_before_refusing(
    client: TestClient, app, monkeypatch
):
    """撞上限时先回收**确实没人要**的行，而不是一律拒绝。

    没有这一步，一个曾经被乱刷过的中心就永久失去了"再加一台设备"的能力 ——
    上限本来是为了防撑爆，结果变成一个自己好不了的坑。
    判据是"未绑定 + 配对早已作废"，所以一台正在反复重试配对的盒子不会被误删。
    """
    monkeypatch.setattr(devices_module, "MAX_DEVICES", 2)
    session = app.state.session_factory()
    try:
        session.add(
            Device(
                hardware_id="junk-1",
                name="没人要的",
                status=DeviceStatus.PENDING,
                pairing_expires_at=utcnow() - timedelta(days=3),
            )
        )
        session.add(
            Device(
                hardware_id="live-1",
                name="还在配对的",
                status=DeviceStatus.PENDING,
                pairing_expires_at=utcnow() + timedelta(seconds=120),
            )
        )
        session.commit()
    finally:
        session.close()

    assert _pair(client, hardware_id="new-1")["code"]  # 撞上限 → 回收 1 行 → 建得下

    session = app.state.session_factory()
    try:
        left = {row.hardware_id for row in session.query(Device).all()}
    finally:
        session.close()
    assert "junk-1" not in left
    assert {"live-1", "new-1"} <= left


def test_the_cap_never_reclaims_a_device_that_has_been_bound_before(
    client: TestClient, app, user_a, monkeypatch
):
    """⭐ 回收**只**能动"从没绑成功过"的行。这条是我自己写第一版时写错的地方。

    第一版的判据是"`status == PENDING` 且配对过期"，理由写在注释里："PENDING 的行
    不可能有任务"。**那个理由不成立**：`bound_at` 一旦写上就再也不清空，
    所以「绑过 → 解绑 → 重新配对」的盒子此刻正是一个 PENDING 的行，而它名下有历史任务。
    删掉它会触发 `tasks.device_id` 的 `ON DELETE SET NULL` ——
    **静默地**把那些任务变成"没有来源"，而这恰恰是解绑时刻意保住的东西
    （解绑不删行的唯一理由就是它）。

    真正的判据不是"现在是不是 PENDING"，而是"**有没有可能拥有过任务**"：
    上报要设备令牌，令牌只在绑定成功时发出，所以 `bound_at is None` ⟹ 不可能有任务。
    可证伪的形式就是这条用例：走一遍真实的 bind → unbind → 再配对，
    把配对过期时刻推到很久以前，再撞上限 —— 必须收到 503，且那一行一个字都不能少。
    """
    monkeypatch.setattr(devices_module, "MAX_DEVICES", 1)
    pairing = _pair(client)
    device = _bind(client, pairing["code"], user_a).json()
    assert client.post(f"/api/devices/{device['id']}/unbind", headers=user_a).status_code == 200

    # 解绑后再配对：这一行回到 PENDING，但 bound_at 还留着（它就是"绑过"的痕迹）
    _pair(client)
    row = _device_row(app, device["id"])
    assert row.status == DeviceStatus.PENDING
    assert row.bound_at is not None, "这条用例的前提：重新配对不会清掉 bound_at"

    # 把它的配对时刻推到很久以前，让它**在旧判据下**确实落在"可回收"里
    session = app.state.session_factory()
    try:
        stale = session.get(Device, device["id"])
        stale.pairing_expires_at = utcnow() - timedelta(days=3)
        session.commit()
    finally:
        session.close()

    blocked = client.post("/api/devices/pair/start", json={"hardware_id": "brand-new"})
    assert blocked.status_code == 503, "绑过的设备不能当垃圾回收掉"

    # 行还在（用"整张表里有哪些行"来断言，而不是 `_device_row`：
    # 行被删掉时那个助手会拿 None 去 expunge 而抛异常，红得不如一句断言清楚）
    session = app.state.session_factory()
    try:
        left = {row.hardware_id for row in session.query(Device).all()}
    finally:
        session.close()
    assert left == {HARDWARE_ID}, "绑过的设备行被回收掉了，历史任务的归属会跟着消失"


def test_rate_limit_buckets_do_not_grow_without_bound(client: TestClient, monkeypatch):
    """限流桶的键是**请求给的东西**，所以它自己也必须封顶。

    `hardware_id` 是 64 字符的自由串：撒不同的串就能让 `_pair_starts_by_hw`
    一直涨。原来的清理只在"有空的桶"时才有东西可删 —— 全是新键时一个也删不掉，
    而且从那以后每次请求都要把整张表遍历一遍（内存和 CPU 一起放大）。

    走 HTTP 而不是直接调 `_too_fast`：要证明的性质是"接口不能被用来喂大进程内存"，
    那就从接口打进去看。上限在这里改小只是为了别真撒 512 个键。
    """
    monkeypatch.setattr(devices_module, "_MAX_BUCKETS", 4)
    for i in range(25):
        client.post("/api/devices/pair/start", json={"hardware_id": f"spray-{i}"})

    assert len(devices_module._pair_starts_by_hw) <= 4


def test_a_looping_box_still_gets_throttled_while_others_spray(
    client: TestClient, monkeypatch
):
    """被淘汰的必须是**撒进来的一次性键**，不能是正在打转的那台盒子。

    这一条钉住"淘汰谁"这个决定 —— 它比看起来更要紧，因为按插入顺序淘汰
    是最省事、也最像对的写法，但那样第一个扔掉的就是**最早插入的那个键**，
    而一台重启打转的盒子恰好就是最早插入的那个：它正是这条限流唯一要拦的对象。
    把它淘汰掉 = 它的窗口被反复清零 = 它永远不会被拦。

    判据只能落在"它还拦不拦得住"上：**只断言它还在字典里是不够的** —
    被淘汰后紧接着的下一次调用会把它重新插回去，"键还在"与"计数还在"是两回事。
    所以这里一直在给那台盒子发请求，断言它最终会吃到一个 429。
    """
    monkeypatch.setattr(devices_module, "_MAX_BUCKETS", 4)
    hot = "loop-box"
    statuses = []
    for i in range(40):
        client.post("/api/devices/pair/start", json={"hardware_id": f"spray-{i}"})
        statuses.append(
            client.post("/api/devices/pair/start", json={"hardware_id": hot}).status_code
        )

    assert 429 in statuses, "一直在打转的盒子被别人的请求洗掉了计数，限流失效"


# ---------------------------------------------------------------- 改名 / 解绑
def test_rename_and_clear_plot(client: TestClient, user_a):
    device = _bind(client, _pair(client)["code"], user_a).json()
    plot = client.post(
        "/api/plots", json={"name": "东头大棚", "crop": "番茄"}, headers=user_a
    ).json()

    # 先**真的**挂上一个地块 —— 否则下面"只传 name 不该抹掉 plot_id"无从判断
    attached = client.patch(
        f"/api/devices/{device['id']}", json={"plot_id": plot["id"]}, headers=user_a
    ).json()
    assert attached["plot_id"] == plot["id"]

    # ⭐ 只传 name：plot_id 必须原样留着。
    # 原先这条用例从头到尾 plot_id 都是 None，"保留"和"抹掉"看起来一模一样 ——
    # 就算 `update_device` 里那个 `"plot_id" in payload.model_fields_set` 被删掉，
    # 它照样是绿的，而现场的表现是"改个名字，设备的地块归属没了"。
    renamed = client.patch(
        f"/api/devices/{device['id']}", json={"name": "东头大棚"}, headers=user_a
    ).json()
    assert renamed["name"] == "东头大棚"
    assert renamed["plot_id"] == plot["id"], "只改名字不该把地块归属抹掉"

    # 显式 null 才是"取消地块归属"
    cleared = client.patch(
        f"/api/devices/{device['id']}", json={"plot_id": None}, headers=user_a
    ).json()
    assert cleared["plot_id"] is None


def test_setting_a_plot_that_does_not_exist_is_a_400(client: TestClient, user_a):
    """地块不存在要回干净的 400，而不是撞外键约束变成 500。"""
    device = _bind(client, _pair(client)["code"], user_a).json()
    response = client.patch(
        f"/api/devices/{device['id']}", json={"plot_id": 999999}, headers=user_a
    )
    assert response.status_code == 400


def test_unbind_releases_the_device_and_kills_its_token(client: TestClient, app, user_a):
    """解绑是权限变更，不是数据销毁：设备行还在，但谁的列表里都不再出现。"""
    pairing = _pair(client)
    device = _bind(client, pairing["code"], user_a).json()
    token = _poll(client, pairing["pairing_secret"])["device_token"]
    assert token

    response = client.post(f"/api/devices/{device['id']}/unbind", headers=user_a)
    assert response.status_code == 200
    assert response.json()["status"] == DeviceStatus.UNBOUND

    assert client.get("/api/devices", headers=user_a).json() == []
    assert client.get(f"/api/devices/{device['id']}", headers=user_a).status_code == 404

    # 归属抹掉；令牌**不再有任何权限**（状态不是 active 了，请求在进路由体之前就被挡下）
    row = _device_row(app, device["id"])
    assert row.owner_id is None
    assert row.last_seen_at is None
    # 但 hash 留着 —— 这样盒子拿旧令牌来能收到"你已被解绑"，而不是含糊的"令牌无效"
    assert row.token_hash is not None
    assert row.pending_token_plain is None
    # 配对凭证是**旧绑定**的东西，跟着一起作废：不清的话，一个还揣着它的
    # 旧代理会在下一次 poll 里被当成这台设备的合法配对者。
    assert row.pairing_secret_hash is None


def test_unbound_device_can_be_rebound_by_someone_else(client: TestClient, user_a, user_b):
    """解绑后重新配对 → 易主。这是"换个人用"的唯一正道。"""
    device = _bind(client, _pair(client)["code"], user_a).json()
    client.post(f"/api/devices/{device['id']}/unbind", headers=user_a)

    rebound = _bind(client, _pair(client)["code"], user_b)
    assert rebound.status_code == 200
    assert rebound.json()["id"] == device["id"]
    assert [d["id"] for d in client.get("/api/devices", headers=user_b).json()] == [device["id"]]
    assert client.get("/api/devices", headers=user_a).json() == []


# ---------------------------------------------------------------- 设备任务列表
def test_device_tasks_are_empty_and_paginated(client: TestClient, user_a):
    device = _bind(client, _pair(client)["code"], user_a).json()
    body = client.get(f"/api/devices/{device['id']}/tasks", headers=user_a).json()
    assert body == {"items": [], "total": 0, "page": 1, "page_size": 20}


# ---------------------------------------------------------------- 待配对列表
def test_pending_list_requires_auth(client: TestClient):
    """敲门名单也要登录：没登录不能知道"有几台盒子正在等人认领"。"""
    _pair(client)
    response = client.get("/api/devices/pending")
    assert response.status_code == 401


def test_pending_list_shows_knocking_box_but_never_the_code(client: TestClient, user_a):
    """最要紧的一条：**响应里绝对不能出现配对码**。

    这个接口本来就是为了告诉网页"有人在敲门"，很容易顺手把 code 一起带上
    （省得人去盒子终端抄）。那就拆掉了整套配对安全模型 —— 码是"物理接触过
    这台盒子"的证明，任何登录用户都能看到码 = 任何人都能绑走别人刚配上的盒子。
    """
    body = _pair(client, name="东头盒子", model="Raspberry Pi 5")
    response = client.get("/api/devices/pending", headers=user_a)
    assert response.status_code == 200

    items = response.json()
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "东头盒子"
    assert item["model"] == "Raspberry Pi 5"
    # 有到期时间（前端据此显示倒计时），但没有码、没有凭证
    assert item["pairing_expires_at"]
    assert set(item) == {
        "id",
        "name",
        "model",
        "agent_version",
        "backend",
        "last_seen_at",
        "pairing_expires_at",
    }
    assert body["code"] not in response.text


def test_pending_list_drops_the_box_once_bound(client: TestClient, user_a):
    """绑完就从名单里消失 —— 横幅不能一直挂着已经进门的那台。"""
    code = _pair(client)["code"]
    before = client.get("/api/devices/pending", headers=user_a).json()
    assert len(before) == 1

    assert _bind(client, code, user_a).status_code == 200
    assert client.get("/api/devices/pending", headers=user_a).json() == []


def test_expired_pairing_is_not_in_pending_list(client: TestClient, user_a, app, monkeypatch):
    """码过期后不该再来敲门。过期是这个接口唯一的清理时机之外的另一半。"""
    # 先把码的有效期捏到负的发动点上：pair/start 时就已经过期
    monkeypatch.setattr(devices_module, "PAIRING_TTL_S", -1)
    _pair(client)
    assert client.get("/api/devices/pending", headers=user_a).json() == []


def test_pending_list_is_not_scoped_to_owner(client: TestClient, user_a, user_b):
    """待配对的盒子还没有主人，两个账号都看得到。

    这不是漏了归属过滤：谁能绑取决于谁能拿到码（物理接触），而"门铃响了"
    这件事本身不是秘密 —— 一个账号看到提示，也抢不走另一个账号已经在用的设备。
    """
    _pair(client)
    assert len(client.get("/api/devices/pending", headers=user_a).json()) == 1
    assert len(client.get("/api/devices/pending", headers=user_b).json()) == 1
