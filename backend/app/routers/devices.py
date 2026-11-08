# -*- coding: utf-8 -*-
"""绑定设备：配对、绑定、解绑，以及设备角度的任务查询。

本文件里混着**两类受众完全不同**的接口，读代码时先分清：

**面向盒子**
    POST /devices/pair/start      盒子报到，拿一个 6 位配对码 + 握手凭证 —— 匿名
    GET  /devices/pair/poll       盒子轮询"我被绑上了吗"，绑上后取走设备令牌 —— 匿名
    POST /devices/tasks           盒子上报一次抓拍的检测结果          —— 设备令牌
  前两个必须匿名：盒子还没有账号，当然也带不了用户令牌。

  但**匿名不等于只认 `hardware_id`**。`hardware_id` 是树莓派的 serial，明写在
  `/proc/cpuinfo` 里，等同于公开信息；而"绑定完成"到"盒子把令牌取走"之间有一小段
  窗口，只凭 `hardware_id` 就能轮询的话，任何知道这串 serial 的人都能赶在真盒子
  之前把设备令牌领走。所以 `pair/start` 额外发一个高熵的 `pairing_secret`，
  `pair/poll` 必须通过 `X-Pairing-Secret` 头带回来 —— 凭证不对时回 `unknown`
  （与"中心不认识这台硬件"逐字相同），不泄漏"这台盒子正在配对"这件事本身。

  匿名还意味着**这条接口不能成为写别人数据的入口**：盒子自报的
  name/model/agent_version/backend 只写进还没有主人的行（见 `pair_start`），
  否则"知道 serial"就等于"能把别人田里那台盒子改名"。同理，它也不能被用来
  无限建行，所以 `devices` 表封顶（`MAX_DEVICES`）。

  顺带说明它**没有**挡住的：任何人都能用别人的 `hardware_id` 调 `pair/start`，
  把对方正在显示的配对码顶掉。这是**首次接触本身**的属性，不是这一层的疏漏 ——
  拦截它并不解决问题，只会把"码被顶掉"换成"先用者占位、真盒子一直等到过期"
  （同样是知道 serial 就能做到，而且连一句错误都不给现场）。真正的出路在更外面：
  盒子所在网络不要暴露这个端口，以及把内容加密（现在是明文 HTTP，
  能调到这条接口的人也能直接嗅到凭证）。见 README 的局限性清单。

**面向网页（需登录，且按 owner_id 过滤）**
    GET/PATCH /devices、GET /devices/{id}
    POST /devices/bind            用户输入配对码完成绑定
    POST /devices/{id}/unbind
    GET  /devices/{id}/tasks

**归属边界（明说，不留含糊）**
设备的「管理 / 详情 / 任务 / 实时流」四个面全部按 `owner_id` 隔离，别人拿不到。
这与**全站同一条规则**一致：有主的只有主人看得见，无主的公共，看得见 = 能删
（定义在 `services/ownership.py`，任务 / 看板 / 告警 / 地块 / 检测产物都走它）。
所以设备上报的任务现在也按账号隔离：B 登录后既枚举不到 A 的树莓派，
也看不到它上报的任务。设备挂地块时同样要过归属校验 —— 否则猜一个 id 就能
把设备挂到别人的私有地块上，此后每次上报都带着那块地的 id。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_current_device, get_current_user, get_db, get_settings
from ..models import Device, DeviceStatus, Plot, Task, User, utcnow
from ..schemas import (
    DeviceBindIn,
    DeviceOut,
    DevicePairPollOut,
    DevicePairStartIn,
    DevicePairStartOut,
    DevicePendingOut,
    DeviceStreamTicketOut,
    DeviceTaskIn,
    DeviceTaskIngestOut,
    DeviceUpdateIn,
    Page,
    TaskOut,
)
from ..security import (
    decode_stream_ticket,
    hash_device_token,
    hash_pairing_secret,
    new_device_token,
    new_pairing_code,
    new_pairing_secret,
    new_stream_ticket,
    verify_pairing_secret,
)
from ..services.device_ingest import ingest_device_task
from ..services.ownership import owner_scope
from ..services.presence import is_online, touch_device
from ..services.storage import sign_task_media_fields

logger = logging.getLogger("insect")

router = APIRouter(prefix="/devices", tags=["devices"])

#: 配对码有效期。5 分钟够人从终端抄到网页；再长就只是白白扩大猜码窗口。
PAIRING_TTL_S = 300
#: 看实时画面的票据有效期。短到即使漏进日志也无所谓，长到够看完一段现场情况。
#: 前端每 90 秒重领一次，所以到期前就会被换掉，观众不会看到画面中断。
STREAM_TICKET_TTL_S = 120
#: 已建立起来的画面流，每隔这么多秒复查一次"设备还在不在、还是不是我绑的"。
#: **只在开流时校验一次是不够的**：解绑后 `hub.forget()` 会被盒子下一帧填回来
#: （那条 WS 还连着），画面会一直播下去；换主之后甚至是在放别人的摄像头。
#: 30 秒一次查询对单机部署可以忽略，却把"解绑后还能看多久"真正钉住了。
STREAM_REVALIDATE_S = 30.0
#: 建议盒子轮询的间隔。短轮询而非 long-poll：盒子本身是个还要重连、要重配对的
#: 循环，long-poll 会在网络超时之上再叠一层超时；而挂住一个线程池 worker 25 秒
#: 在 4 核树莓派上也是实打实的开销。
POLL_INTERVAL_S = 2

#: `devices` 表的总行数上限。这张表**只增不减**（解绑也刻意不删行，历史任务要靠它），
#: 而"建行"的入口 `pair/start` 是匿名的 —— 不封顶的话，一个能连到中心的人
#: 就等于能往树莓派的 SD 卡里写任意多行。2000 是给"一个农场十几台"留了三个数量级的余量：
#: 真撞上这个数，该看的是日志，而不是继续建行。
#: （与 frame_hub 的 `max_devices` 同一种护栏：宁可对未知来源硬顶，也不让它无限长。）
MAX_DEVICES = 2000
#: 未绑定设备被判为"没人要"的闲置时长。撞上限时按这个标准回收 ——
#: 判据取 `pairing_expires_at` 而不是 `created_at`，理由见 `_reclaim_stale_pending`。
STALE_PENDING_S = 24 * 3600

# 在线判据、心跳节流都在 services/presence.py —— 那里是两个调用方共用的口径

# ---- 进程内限流 ----------------------------------------------------------
# 与 app.state.active_streams 同一种思路：单实例部署下够用的轻量护栏，不引依赖。
# **扛不住多 worker 部署**（每个 worker 各有一份计数）—— 本方案的目标是
# 单实例局域网/单机部署，可以接受；真要横向扩就得换成共享存储。
_ATTEMPT_WINDOW_S = 600.0        # 统计窗口：10 分钟
_MAX_ATTEMPTS_PER_USER = 10      # 每个用户在这个窗口内**最多试 10 次**（第 11 次 429）
_MAX_PAIR_START_PER_HW = 12.0    # 同一台盒子每分钟最多发起 12 次配对（防重启打转）
#: 限流桶的**键数**上限。键来自请求（`hardware_id` 是 64 字符的自由串），
#: 撒不同的键就能让这个字典一直涨，所以必须封顶 —— 见 `_too_fast`。
_MAX_BUCKETS = 512

_guard_lock = threading.Lock()
_attempts_by_user: dict[int, deque[float]] = {}
_pair_starts_by_hw: dict[str, deque[float]] = {}
#: 轮询凭证不匹配的**日志**闸门（不是请求闸门）。键是 hardware_id，每分钟最多 1 条。
#: 只压日志，不压请求：盒子本来就每 2 秒轮一次，给这条接口限流会把正常配对一起打死。
_MAX_POLL_MISMATCH_LOGS_PER_MIN = 1.0
_poll_mismatch_logs: dict[str, deque[float]] = {}

# 关于"猜配对码"，实际设防的是这三样，别在别处再脑补第四样：
#   1. 10⁶ 的码空间 × 300 秒 TTL；
#   2. 上面那条**每账号 10 次 / 10 分钟**的限流 —— 一个账号在一个码的有效期内
#      最多猜 5 次，命中概率 5×10⁻⁶；
#   3. 绑定成功即清码，同一个码绑不了第二次。
#
# 曾经这里还有一个"同一个码连续失败 5 次就把它作废"的计数器，**已删除**：
# 它按"提交上来的码"去查设备，可失败的猜测按定义就没匹配上任何设备的码，
# 于是永远查不到东西、永远不生效 —— 一个注释说在防护、实际什么也没做的机制，
# 比没有这个机制更糟。而且它想防的事本来就防不了：攻击者是随机撒码的，
# 我们无从知道他在瞄哪台设备，也就没有"那个码"可以作废。
#
# 诚实记下**没有**设防的部分：限流是按账号算的，所以注册大量小号分布式地猜
# 是绕得过去的。这一层留到真上公网时用共享存储 + 注册限流一起做（见 README 的
# 局限性清单）。当前目标是单农场局域网，先把形状做对。


def _too_fast(bucket: dict, key, limit: float, window: float, now: float) -> bool:
    """记一次事件，返回"是否已超限"。

    超限时**不记录**，否则一旦触发就再也退不出来（时间戳永远堆在窗口里）。
    """
    with _guard_lock:
        stamps = bucket.setdefault(key, deque[float]())
        while stamps and stamps[0] < now - window:
            stamps.popleft()
        if len(stamps) >= limit:
            return True
        stamps.append(now)
        if len(bucket) > _MAX_BUCKETS:
            _trim_buckets(bucket)
        return False


def _trim_buckets(bucket: dict) -> None:
    """把限流桶的键数压回上限之内。**键是请求给的东西**，所以不能不管。

    这里曾经只清"空桶"，而那件事根本框不住字典：撒不同的 `hardware_id` 时
    每个桶里都有一条刚记进去的时间戳、一个都不空，于是一个也删不掉；
    更糟的是超过判断阈值之后每次请求都要把整张表遍历一遍，
    在被人撒键的情况下那本身就是个 O(n²) 的放大器。

    淘汰**不按插入顺序**：一台正在打转的盒子恰好是最早插入的那个键，
    按插入顺序扔，第一个被扔的就是它 —— 而它正是这条限流要拦的对象。
    改成按"最后一次事件的时间"淘汰最久没用过的：撒进来的键各只有一次事件，
    时间戳冻在那一刻，先出局的是它们。

    一次削到 3/4 而不是刚好削到上限：否则每来一个新键都要遍历一遍整张表。
    代价是 fail-open —— 被淘汰的键限流历史归零，可能少拦一次。认了：
    这是防"盒子重启打转"的护栏，不是安全边界（它本来就扛不住多 worker、也扛不住换账号刷）。
    """
    for stale in [k for k, v in bucket.items() if not v]:
        del bucket[stale]
    if len(bucket) <= _MAX_BUCKETS:
        return
    target = _MAX_BUCKETS * 3 // 4
    for key, stamps in sorted(bucket.items(), key=lambda kv: kv[1][-1])[: len(bucket) - target]:
        del bucket[key]


def _clear_bucket(bucket: dict, key) -> None:
    with _guard_lock:
        bucket.pop(key, None)


# ---- 小工具 --------------------------------------------------------------
def _normalize_code(raw: str) -> str:
    """只留数字。用户在网页上可能把「381 402」连空格一起粘进来。"""
    return "".join(ch for ch in raw if ch.isdigit())


def _to_out(device: Device, hub, now: datetime) -> DeviceOut:
    """ORM 对象 -> 响应模型，补上两个派生字段（它们不落库，见 DeviceOut 的注释）。"""
    out = DeviceOut.model_validate(device)
    out.online = is_online(device, now)
    out.stream_fps = hub.fps_of(device.id) if hub is not None else 0.0
    return out


def _hub(request: Request):
    return getattr(request.app.state, "frame_hub", None)


def _owned_device_or_404(db: Session, device_id: int, user: User) -> Device:
    """取出属于该用户的设备。

    **不是我的就 404，不是 403**：403 等于承认"这个 id 存在，只是不归你"，
    那本身就是在泄漏别人的设备清单。统一 404，也让前端只需处理一种"没有"。
    """
    device = db.get(Device, device_id)
    if device is None or device.owner_id != user.id or device.status != DeviceStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    return device


def _hand_over_token(db: Session, device: Device) -> DevicePairPollOut:
    """把待发的明文令牌交出去，并且**只交这一次**。

    认领用的是条件 UPDATE 而不是"读出来、改掉、提交"：两个并发的 poll
    （同一个凭证、两路进程、或者一个会重放的代理）都会先读到同一个明文，
    然后各自交一遍 —— 那样 `只交这一次` 就只是"一般情况下只交一次"。
    加上 `pending_token_plain IS NOT NULL` 这个条件之后，谁先提交谁拿到，
    后一个的 `rowcount` 是 0，于是它什么都拿不到。
    （这也顺带说明为什么不能只用 ORM 写回：那样两个请求都能看见旧值。）
    """
    token = device.pending_token_plain or ""
    claimed = db.execute(
        update(Device)
        .where(Device.id == device.id, Device.pending_token_plain.is_not(None))
        .values(
            pending_token_plain=None,
            # 连同"谁有权领它"的那份快照一起清掉 —— 两者同生同灭
            pending_token_secret_hash=None,
            # 顺便让当前这枚凭证也失效：它的使命就是"把令牌安全地送到"，送到即收回
            pairing_secret_hash=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if not claimed.rowcount:
        # 另一路刚刚取走了。**不能**回 paired：那会让盒子把一个已经不存在的令牌
        # 当成自己的。回 unknown 会让它重新配对，那是这里正确的下一步。
        return DevicePairPollOut(status="unknown", device_id=device.id)
    return DevicePairPollOut(
        status="paired", device_id=device.id, name=device.name, device_token=token
    )


# ---- 面向盒子：配对（匿名）------------------------------------------------
def _new_device(db: Session, hardware_id: str, name: str) -> Device:
    """为新 `hardware_id` 建一行，但先顶住"匿名刷行"。

    `devices` 只增不减（解绑也刻意不删行，历史任务要靠它），而建行的入口是匿名的 ——
    没有上限的话，"能连到中心"就等于"能往树莓派的 SD 卡里写任意多行"。
    所以封顶；封顶时先回收**确实没人要**的未绑定行（`_reclaim_stale_pending`），
    再不行就明确拒绝并**记一条日志** —— 静默地继续建行是最坏的那条路，
    它连一句"这里出事了"都不留。
    """
    if db.query(Device).count() >= MAX_DEVICES:
        freed = _reclaim_stale_pending(db)
        if db.query(Device).count() >= MAX_DEVICES:
            logger.warning(
                "设备表已到上限 %s 行（本次回收了 %s 行陈旧的未绑定设备），"
                "拒绝为 hardware_id=%s 建新行",
                MAX_DEVICES,
                freed,
                hardware_id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="中心记录的设备数量已达上限，请先清理未绑定的设备",
            )
    device = Device(
        hardware_id=hardware_id,
        name=name or f"设备 {hardware_id[-6:]}",
        status=DeviceStatus.PENDING,
    )
    db.add(device)
    return device


def _reclaim_stale_pending(db: Session) -> int:
    """删掉"从没绑成功、配对也早已作废"的设备行，返回删掉的行数。

    **只删"从没绑成功过"的 `PENDING` 行**，两个条件缺一不可：

    - `UNBOUND` 的行是历史任务 `device_id` 的指向对象（解绑刻意不删行就是为了它），
      删掉会让历史的设备归属凭空消失。
    - `PENDING` 但 `bound_at` 非空的行同理 —— 这条容易看漏（我第一版就写错了）。
      `bound_at` 一旦写上就不再清空，所以"绑过又解绑、然后重新配对"的盒子
      此刻正是一个 `PENDING` 且 `bound_at` 非空的行，而它名下有历史任务。
      删它会触发 `tasks.device_id` 的 `ON DELETE SET NULL`，
      **静默地把那些任务变成"没有来源"** —— 正是解绑时刻意保住的东西。
      真正的判据不是"现在是不是 PENDING"，而是"**有没有可能拥有过任务**"：
      上报要设备令牌，令牌只在绑定成功时发出，所以 `bound_at is None`
      ⟹ 这台盒子从来没拿到过令牌 ⟹ 不可能有任务。反过来不成立。

    这个收紧不会让垃圾行堆积，也就不会把上限变成"填满之后再也好不了"的坑：

    - 见过一次的 `hardware_id` 再次 `pair/start` 会**复用原来那一行**（按
      `hardware_id` 查），不会因为不回收就多出一行；
    - 唯一会留下的新行是"绑成功过、之后解绑了"的 —— 而绑定必须由登录用户在网页上
      输入一个有效的 6 位码，接口还按账号限流。它的增长速度由人的操作决定，
      不受匿名流量影响（这正是要防的那种增长）。

    判据取 `pairing_expires_at` 而不是 `created_at`：一台配对老失败、一直在重试的盒子
    `created_at` 很老但配对是新的 —— 那种行不是垃圾，不能回收。
    """
    cutoff = utcnow() - timedelta(seconds=STALE_PENDING_S)
    stale = db.scalars(
        select(Device).where(
            Device.status == DeviceStatus.PENDING,
            Device.bound_at.is_(None),
            or_(Device.pairing_expires_at.is_(None), Device.pairing_expires_at < cutoff),
        )
    ).all()
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
    return len(stale)


@router.post("/pair/start", response_model=DevicePairStartOut)
def pair_start(
    payload: DevicePairStartIn,
    request: Request,
    db: Session = Depends(get_db),
) -> DevicePairStartOut:
    """盒子报到，拿一个 6 位配对码。

    对**已经绑定过**的盒子也照常发码：盒子无从知道自己被绑了，
    在这里拒绝只会让它的终端上出现一条没法解释的错误。让它拿到码、
    让人在网页上看到「该设备已被绑定，请先解绑」，才是能自解释的路径。

    因为匿名，这条路要自己管住三件事（都在本文件里就近写着理由）：
    发起频率（`_MAX_PAIR_START_PER_HW`）、**不往别人的设备行上写自报字段**、
    以及不为未知 `hardware_id` 无限建行（`MAX_DEVICES`）。
    """
    now_mono = datetime.now().timestamp()
    hardware_id = payload.hardware_id.strip()
    if _too_fast(_pair_starts_by_hw, hardware_id, _MAX_PAIR_START_PER_HW, 60.0, now_mono):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="配对请求过于频繁，请稍后再试"
        )

    device = db.scalars(select(Device).where(Device.hardware_id == hardware_id)).first()
    if device is None:
        device = _new_device(db, hardware_id, payload.name)

    if device.status == DeviceStatus.UNBOUND:
        # 曾被解绑，现在重新配对：回到 pending，等新主人认领
        device.status = DeviceStatus.PENDING
        device.owner_id = None

    # 盒子自报的这几个字段**只在设备还没有主人时**才写。
    # 这条接口是匿名的，而 `hardware_id` 是能从 /proc/cpuinfo 读出来的串 ——
    # 不设这道门，"知道 serial"就等于"能把别人已经绑好的设备改名"
    # （`model` / `agent_version` / `backend` 同理，都是往别人的那行上写字）。
    # 正常的重新配对不受影响：解绑会把 `owner_id` 置空，所以无主设备的自报字段照收。
    if device.owner_id is None:
        if payload.name:
            device.name = payload.name
        if payload.model:
            device.model = payload.model
        if payload.agent_version:
            device.agent_version = payload.agent_version
        if payload.backend:
            device.backend = payload.backend

    device.pairing_code = new_pairing_code()
    device.pairing_expires_at = utcnow() + timedelta(seconds=PAIRING_TTL_S)
    secret = new_pairing_secret()
    device.pairing_secret_hash = hash_pairing_secret(secret)
    # 上一轮配对留下的明文令牌要清掉，否则盒子可能拿到一个属于旧绑定的令牌。
    # **只清不是 active 的**：active 设备上还留着明文令牌，只可能意味着
    # "刚好绑上、盒子还没来取" —— 那不是旧绑定的残留，而是**正在进行中的交接**，
    # 这时候擦掉它，盒子就再也拿不到令牌、只能一直重新配对。
    if device.status != DeviceStatus.ACTIVE:
        device.pending_token_plain = None
    db.commit()
    db.refresh(device)

    return DevicePairStartOut(
        pairing_id=device.id,
        code=device.pairing_code or "",
        pairing_secret=secret,
        expires_in=PAIRING_TTL_S,
        poll_interval_s=POLL_INTERVAL_S,
    )


@router.get("/pair/poll", response_model=DevicePairPollOut)
def pair_poll(
    hardware_id: str = Query(min_length=1, max_length=64),
    x_pairing_secret: str | None = Header(default=None, alias="X-Pairing-Secret"),
    db: Session = Depends(get_db),
) -> DevicePairPollOut:
    """盒子轮询配对结果。

    机器可读的状态只有四种，盒子对每一种都有明确的下一步：
      pending  继续等
      paired   拿到令牌，落盘，开始工作（令牌**这一次**有效，取走即清）
      expired  码过期了，重新 pair/start 并打印新码
      unknown  中心不认识这台硬件（或它已被别人绑走），重新 pair/start

    必须带 `X-Pairing-Secret`（`pair/start` 发的那一枚）。**凭证不对一律回
    `unknown`**，与"中心不认识这台硬件"逐字相同 —— 这样这条接口对外就只是
    "要么接着等、要么什么都没有"，猜 `hardware_id` 的人既拿不到令牌，
    也确认不了这台盒子是不是正在配对。

    有一处例外，而且是刻意留的：**待交付的令牌只认绑定那一刻的凭证**
    （`pending_token_secret_hash`），所以一个在绑定之后被人用 `pair/start`
    换掉凭证的盒子，仍然能凭手里那枚拿到令牌。不这么做的话，知道 serial 的人
    只要在"绑定完成 → 盒子来取"之间自己发一次 `pair/start`，就顶替了领取者 ——
    而那正是这枚凭证要防的事（见 models 里那一列的注释）。

    走**头**而不是 query：query 会进访问日志（同 `ws/agent.py` 里令牌不进 query 的理由）。
    代价是发凭证与用凭证的**版本必须一起升** —— 老代理不带这个头，
    会被一直回 `unknown` 而不是某条明确的错误，所以代理侧在不匹配时会直接报
    "中心与代理版本不一致"（见 edge_agent.py 的 `_start_pairing`）。
    """
    device = db.scalars(select(Device).where(Device.hardware_id == hardware_id.strip())).first()
    if device is None:
        return DevicePairPollOut(status="unknown")

    # 代发的令牌**先查**：它只认绑定那一刻的那枚凭证（`pending_token_secret_hash`）。
    # 顺序是有意的 —— 盒子手里的凭证被别人的 `pair/start` 顶掉之后，它依然是这台设备
    # 令牌的合法领取者；若把它判成 `unknown`，它会清掉本地状态重新配对，
    # 而就在那个来回里，令牌落到了发起那轮配对的人手上（匿名、只凭 serial 就能发起）。
    if device.pending_token_plain and verify_pairing_secret(
        x_pairing_secret, device.pending_token_secret_hash
    ):
        return _hand_over_token(db, device)

    if not verify_pairing_secret(x_pairing_secret, device.pairing_secret_hash):
        # 凭证不对与设备不存在**给同一个回答**（见 docstring），但日志里要留痕：
        # 这条接口的正常轮询是不会走到这里的，所以它一出现就说明"有人拿着
        # hardware_id 在猜"或者"盒子与中心版本不一致"，两种都得有人看见。
        # 但**按硬件每分钟只记一条**：它可能被按轮询频率（2 秒一次）重复触发，
        # 也能被人拿着一个已知 serial 故意刷，而每秒一行日志会把它想提示的人埋掉。
        # 复数的成因见 _MAX_POLL_MISMATCH_LOGS_PER_MIN。
        if not _too_fast(_poll_mismatch_logs, hardware_id, 1.0, 60.0, datetime.now().timestamp()):
            logger.warning(
                "配对轮询的凭证不匹配（hardware_id=%s，设备 %s）；"
                "同一硬件每分钟只记一条，重复的已折叠",
                hardware_id,
                device.id,
            )
        return DevicePairPollOut(status="unknown")

    if device.status == DeviceStatus.PENDING:
        if device.pairing_expires_at is not None and device.pairing_expires_at > utcnow():
            return DevicePairPollOut(status="pending", device_id=device.id, name=device.name)
        # 码没了或过期了 —— 把残留的码清掉，免得它还能被 bind 命中
        if device.pairing_code is not None:
            device.pairing_code = None
            device.pairing_expires_at = None
            db.commit()
        return DevicePairPollOut(status="expired", device_id=device.id)

    # ACTIVE 但已经没有待发的令牌：说明令牌早就被取走了（或者被别人绑了）
    return DevicePairPollOut(status="unknown", device_id=device.id)


# ---- 面向盒子：上报检测结果（设备令牌）------------------------------------
@router.post("/tasks", response_model=DeviceTaskIngestOut)
def ingest_task(
    payload: DeviceTaskIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    device: Device = Depends(get_current_device),
) -> DeviceTaskIngestOut:
    """盒子上报一次定时抓拍。

    **设备身份只从令牌来**，路径与请求体里都没有设备 id ——
    否则任何一台盒子都能冒充另一台往别人的账号里写数据。
    归属（`user_id` / `plot_id`）由中心按 `device` 这一行自己填，
    同样不接受客户端指定。

    幂等详见 `services/device_ingest.py`：重发同一条 `seq` 会拿回**同一条**
    结果（`duplicate=true`），而不会在历史里多出一条。
    """
    # 收到上报本身就是一次心跳：盒子哪怕一个任务都没抓，也该显示在线
    touch_device(db, device, min_interval_s=settings.agent.heartbeat_s)
    return ingest_device_task(db, settings, device, payload)


# ---- 面向网页：绑定与解绑 -------------------------------------------------
@router.post("/bind", response_model=DeviceOut)
def bind_device(
    payload: DeviceBindIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DeviceOut:
    """用户输入 6 位配对码，把对应的盒子绑到当前账号。

    这里最要紧的一条安全性质是：**拿到配对码 ≠ 能抢走别人的设备**。
    已绑定的设备必须先由原主在网页上解绑，再配对才能易主。
    """
    now = utcnow()
    if _too_fast(_attempts_by_user, user.id, _MAX_ATTEMPTS_PER_USER, _ATTEMPT_WINDOW_S,
                 datetime.now().timestamp()):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="尝试次数过多，请 10 分钟后再试",
        )

    code = _normalize_code(payload.code)
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请输入配对码")

    # 在 Python 里逐个常量时间比对，而不是让 SQL 做等值查询：
    # 等值查询的耗时随匹配位置变化，理论上能被用来逐位试探。
    candidates = db.scalars(
        select(Device).where(
            Device.pairing_code.is_not(None), Device.pairing_expires_at.is_not(None)
        )
    ).all()
    device = None
    for candidate in candidates:
        if _const_eq(candidate.pairing_code or "", code):
            device = candidate
            break

    if device is None or device.pairing_expires_at is None or device.pairing_expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="配对码无效或已过期"
        )

    if device.status == DeviceStatus.ACTIVE:
        # 码是对的，但这台盒子已经名花有主
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该设备已被绑定，请先在设备页面解绑",
        )

    token = new_device_token()
    device.owner_id = user.id
    device.status = DeviceStatus.ACTIVE
    device.bound_at = now
    device.token_hash = hash_device_token(token)
    # 明文暂存，等盒子的下一次 poll 取走（取走即清）。落库而非放内存，
    # 是因为"盒子配到一半重启"是明确要支持的场景。
    device.pending_token_plain = token
    # **把此刻的凭证连同令牌一起快照下来**：谁把这次配对做到最后，令牌就交给谁。
    # 另外存一份而不是只留 `pairing_secret_hash`，是因为后者会被后续匿名的
    # `pair/start` 换掉 —— 那样"知道 serial 就能领走令牌"（见 models 里那一列的注释）。
    device.pending_token_secret_hash = device.pairing_secret_hash
    device.pairing_code = None
    device.pairing_expires_at = None
    # `pairing_secret_hash` **刻意留着**：盒子还要拿它证明自己是"绑定那一刻的配对者"
    # （盒子重启后第一次 poll 用的就是这枚）。在这里清掉，等于盒子永远取不走令牌。
    # 它会在令牌交付的那一刻失效（见 pair_poll），解绑时也会清。
    db.commit()
    db.refresh(device)

    _clear_bucket(_attempts_by_user, user.id)
    return _to_out(device, _hub(request), now)


def _const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


@router.get("", response_model=list[DeviceOut])
def list_devices(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DeviceOut]:
    """当前账号下已绑定的设备。**只列自己的**。"""
    now = utcnow()
    hub = _hub(request)
    rows = db.scalars(
        select(Device)
        .where(Device.owner_id == user.id, Device.status == DeviceStatus.ACTIVE)
        .order_by(Device.id)
    ).all()
    return [_to_out(row, hub, now) for row in rows]


@router.get("/pending", response_model=list[DevicePendingOut])
def list_pending_devices(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DevicePendingOut]:
    """正在等待配对的盒子（「我的设备」页的敲门提示）。

    解决的体验缺口：以前盒子发起配对后，中心这边**毫不知情** —— 用户必须
    自己去盒子的终端抄码，才知道有人在敲门。现在列表页能直接显示
    "有 N 台盒子正在等待绑定"。

    为什么需要登录却**不分账号**：待配对的盒子还没有主人，谁拿到码谁就可以
    绑（`bind` 本来就是这么设计的）；这里展示的只是"门铃响了"，不含任何凭证。

    为什么限 10 条：正常情况下同时敲门的盒子就是个位数；这里没有分页参数，
    防的是异常场景（被刷 pair/start 的 DoS）下这个接口变成全表扫描。
    """
    now = utcnow()
    rows = db.scalars(
        select(Device)
        .where(
            Device.status == DeviceStatus.PENDING,
            Device.pairing_code.is_not(None),
            Device.pairing_expires_at.is_not(None),
            Device.pairing_expires_at > now,
        )
        .order_by(Device.last_seen_at.desc().nulls_last(), Device.id)
        .limit(10)
    ).all()
    return [
        DevicePendingOut(
            id=row.id,
            name=row.name,
            model=row.model,
            agent_version=row.agent_version,
            backend=row.backend,
            last_seen_at=row.last_seen_at,
            pairing_expires_at=row.pairing_expires_at or now,
        )
        for row in rows
    ]


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DeviceOut:
    return _to_out(_owned_device_or_404(db, device_id, user), _hub(request), utcnow())


@router.patch("/{device_id}", response_model=DeviceOut)
def update_device(
    device_id: int,
    payload: DeviceUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DeviceOut:
    device = _owned_device_or_404(db, device_id, user)
    if payload.name is not None:
        device.name = payload.name
    # plot_id 允许显式传 null（取消地块归属），所以用 `"plot_id" in fields_set`
    # 而不是 `is not None` —— 否则永远取消不掉。
    if "plot_id" in payload.model_fields_set:
        # 外键本身有 `PRAGMA foreign_keys=ON` 兜着，但撞上去是 500。先自己查一次，
        # 好给登录用户一个干净的 400 —— 以后前端加地块选择器时也要靠它。
        #
        # **必须连归属一起查**：只看"行存不存在"的话，猜一个 id 就能把设备挂到
        # 别人的私有地块上（此后这台设备上报的任务都会带上那块地的 id），
        # 且 400 与成功的差别本身就是一条"这个 id 存在"的探针。
        # 无主的地块（历史数据）仍是公共的，与 services/ownership.py 同一口径。
        if payload.plot_id is not None:
            plot = db.get(Plot, payload.plot_id)
            if plot is None or (plot.owner_id is not None and plot.owner_id != user.id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="地块不存在"
                )
        device.plot_id = payload.plot_id
    db.commit()
    db.refresh(device)
    return _to_out(device, _hub(request), utcnow())


@router.post("/{device_id}/unbind", response_model=DeviceOut)
def unbind_device(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DeviceOut:
    """解绑：把设备交还给"无主"状态。

    刻意**不删**这一行，也**不删**它的历史任务 —— 解绑是权限变更，不是数据销毁。
    任务上的 `device_id` 与 `user_id` 都留着，历史照旧查得到。
    盒子会在下一次心跳时收到 403，然后自己清掉令牌、重新进入配对。
    """
    device = _owned_device_or_404(db, device_id, user)
    device.status = DeviceStatus.UNBOUND
    device.owner_id = None
    # `token_hash` **故意留着**。抹掉它的话，这台盒子再拿旧令牌来，
    # 中心就查不到任何行，只能回一句"令牌无效" —— 而真正发生的事是
    # "有人把你解绑了，去重新配对"。留着 hash 才能让 `get_current_device`
    # 走到那个 403 分支，给出能让现场看懂的那句话。
    # 留着不等于还有权限：状态已经不是 active，403 在任何路由体之前就抛出去了。
    device.pending_token_plain = None
    device.pairing_code = None
    device.pairing_expires_at = None
    # 配对凭证是"旧绑定"的东西，跟着令牌一起作废。不清的话，一个还揣着它的
    # 旧代理会在下一次 poll 里被认成这台设备的合法配对者。
    # `pending_token_secret_hash` 是它的快照（`bind_device` 时抄的那一份），
    # 同样是"这次绑定"的凭证，必须一起清 —— 只清一个等于留了把备用钥匙。
    device.pairing_secret_hash = None
    device.pending_token_secret_hash = None
    device.last_seen_at = None
    db.commit()
    db.refresh(device)

    hub = _hub(request)
    if hub is not None:
        # 立刻把画面丢掉，否则解绑后别人还能从内存里看到最后一帧
        hub.forget(device.id)

    now = utcnow()
    return _to_out(device, hub, now)


@router.post("/{device_id}/stream-ticket", response_model=DeviceStreamTicketOut)
def create_stream_ticket(
    device_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
) -> DeviceStreamTicketOut:
    """签发一张看画面的短期票据。

    要票据而不是直接用用户 JWT，是因为 `<img src="...">` **没法设 `Authorization` 头**，
    令牌只能进 query string，而 query 会落进访问日志、浏览器历史和 Referer。
    12 小时的用户令牌那样到处撒是不可接受的；120 秒、且只对着一台设备的票据，
    泄漏了也只是"能看两眼这台摄像头"。
    """
    _owned_device_or_404(db, device_id, user)
    ticket, expires_in = new_stream_ticket(
        user.id, device_id, settings.resolved_jwt_secret, ttl_s=STREAM_TICKET_TTL_S
    )
    return DeviceStreamTicketOut(ticket=ticket, expires_in=expires_in)


def _still_streamable(session_factory: Any, device_id: int, owner_id: int) -> bool:
    """这台设备现在还能不能看：存在、仍是 active、并且还是同一个主人的。

    **同步函数**，调用方一律用 `asyncio.to_thread` 丢出去 —— 生成器跑在事件循环
    上，直接查库会把整条循环卡住。这里只开一次短会话，绝不让它跟着流的寿命走
    （那正是 `Depends(get_db)` 会闯的祸）。
    """
    db = session_factory()
    try:
        device = db.get(Device, device_id)
        return (
            device is not None
            and device.status == DeviceStatus.ACTIVE
            and device.owner_id == owner_id
        )
    finally:
        db.close()


@router.get("/{device_id}/stream")
async def stream_device(
    device_id: int,
    request: Request,
    ticket: str = Query(min_length=1),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """这台设备的实时画面（MJPEG）。

    **鉴权走票据，不走用户 JWT** —— 见 `create_stream_ticket`。
    票据本身绑定了 user 与 device，所以这里不必再要一个用户身份。

    **刻意不依赖 `get_db`。** 这条响应的寿命等于观众看多久，而 FastAPI 的
    yield 依赖要等响应彻底结束才析构 —— 挂上它，每个观众就会**一直占住一个
    数据库连接**，十几路观众足以抽干连接池，之后中心的所有请求一起排队超时。
    所以这里只在开流时开一次短会话，之后每 `STREAM_REVALIDATE_S` 秒复查一次。

    关于"解绑后画面还能看多久"：**由 `STREAM_REVALIDATE_S` 决定，最多 30 秒。**
    不能靠票据的 120 秒 TTL —— 票据只在开流时校验一次，而生成器是照着内存里的
    `frame_hub` 发的，`hub.forget()` 也挡不住（盒子那条 WS 还连着，下一帧就把
    画面填了回来）。所以必须由这一路自己定期复查，否则解绑后画面会一直播下去；
    而换主的情况更糟 —— 放的是**别人**的摄像头。
    """
    try:
        ticket_user_id = decode_stream_ticket(
            ticket, device_id, settings.resolved_jwt_secret, settings.auth.algorithm
        )
    except pyjwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="票据无效或已过期"
        ) from None

    # 开流时开一次短会话，校验完立刻还回去（**不**挂在整个响应的生命周期上）
    session_factory = request.app.state.session_factory
    if not await asyncio.to_thread(
        _still_streamable, session_factory, device_id, ticket_user_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")

    hub = _hub(request)
    if hub is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="推流服务未就绪"
        )

    boundary = "ipframe"
    fps = max(1.0, float(settings.agent.stream_fps))
    interval = 1.0 / fps

    async def frames():
        """**必须是 `async def`。** 写成同步生成器的话，Starlette 会把它丢进
        线程池，于是**每个观众常驻占住一个 worker 线程** —— 在 1~2 个 worker 的
        部署上两三个观众就能把整个后端卡死。`asyncio.sleep` 才让出事件循环。

        生成器只读 `latest()` 并比对 `seq`：没有新帧就发一个空块，
        **从不重放历史帧**。这就是完整的丢帧策略 —— 慢观众看到的是"现在"，
        而不是越拖越久的一段过去。
        """
        last_seq = -1
        next_check = time.monotonic() + STREAM_REVALIDATE_S
        while True:
            if await request.is_disconnected():
                break
            # 定期复查归属：解绑或换主之后，这一路必须自己停下（见 docstring）
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + STREAM_REVALIDATE_S
                if not await asyncio.to_thread(
                    _still_streamable, session_factory, device_id, ticket_user_id
                ):
                    break
            frame = hub.latest(device_id)
            if frame is not None and frame.seq != last_seq:
                last_seq = frame.seq
                yield (
                    b"--" + boundary.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame.jpeg)).encode() + b"\r\n\r\n"
                    + frame.jpeg + b"\r\n"
                )
            else:
                # 空块有两个作用：让中间的代理不在帧间隙掐掉这条连接，
                # 也保证断连检查按 tick 频率真的跑到
                yield b""
            await asyncio.sleep(interval)

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={
            # 票据在 URL 里，别让它随 Referer 漏到第三方站点去
            "Referrer-Policy": "no-referrer",
            # 画面是内存里的实时流，任何缓存都只会让人看到旧的
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.get("/{device_id}/tasks", response_model=Page[TaskOut])
def list_device_tasks(
    device_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> Page[TaskOut]:
    """这台设备的检测结果。归属先校验，再按 device_id 过滤。

    **只校验设备归属是不够的**：设备会换主人（A 解绑 → B 绑定同一台盒子），
    而解绑时历史任务按设计保留（`user_id` 仍是 A）。只按 `device_id` 查，
    新主人 B 就会在这台设备的详情页里看到 A 的历史识别结果 ——
    包括按 B 的身份签发的产物地址，等于把 A 的数据整包交出去。
    所以这里再按任务的归属收一次（与 `/api/tasks` 同一口径）。
    无主的任务仍然公共，与 `services/ownership.py` 的规则一致。
    """
    _owned_device_or_404(db, device_id, user)

    query = db.query(Task).filter(
        Task.device_id == device_id, owner_scope(Task.user_id, user)
    )
    total = query.count()
    rows = (
        query.order_by(Task.started_at.desc(), Task.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    # 与 /api/tasks 同一条规则：产物地址出接口前必须带媒体票据
    items = [
        sign_task_media_fields(
            TaskOut.model_validate(row), task_id=row.id, user_id=user.id, settings=settings
        )
        for row in rows
    ]
    return Page[TaskOut](items=items, total=total, page=page, page_size=page_size)
