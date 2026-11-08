# -*- coding: utf-8 -*-
"""口令哈希、JWT 与设备令牌。

三个刻意的设计决定：

1. bcrypt 前先做 `sha256 -> base64`。bcrypt 只吃前 72 字节，
   直接传长口令会被静默截断（不同库行为还不一致）。
   统一预哈希后，任意长度口令都能正确参与运算，且哈希与校验两侧完全对称。

2. 旧库兼容：`UserDatabase.db` 存的是**无盐 SHA-256（hex64）**。
   由于哈希单向，无法直接转成 bcrypt，只能「旧算法校验 + 登录成功时透明升级」。
   见 `legacy_sha256()` 与 routers/auth.py 的登录流程。

3. **设备令牌用 sha256 而非 bcrypt**。设备令牌是 `secrets` 生成的 256 位随机串，
   没有字典空间可言；bcrypt 的价值在于抬高弱口令的爆破成本，对高熵随机串毫无增益，
   却会让每次心跳多花 ~100ms。这一条曾随设备接入层移除，现按"账号绑定设备"的需求接回。

4. **`<img>` / `<video>` 无法带 Authorization 头**，所以另发"票据"：
   短寿命、用途隔离、对象隔离的 JWT，只能放进 query string。具体有两种，
   **用途互不相通**（拿票据当登录令牌也不成立，见 `decode_access_token`）：
   * `new_stream_ticket` —— 看一台设备的 MJPEG 实时画面（120 秒）；
   * `new_task_media_ticket` —— 取一条任务的标注图 / 原图 / 标注视频（半小时窗口）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

# bcrypt.gensalt 的 cost。12 是安全与耗时的常用平衡点。
_BCRYPT_ROUNDS = 12

# bcrypt 的硬限制
_BCRYPT_MAX_BYTES = 72


def _prehash(password: str) -> bytes:
    """把任意长度口令压成固定 44 字节，规避 bcrypt 的 72 字节截断问题。"""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # 哈希格式非法（例如被手工改过）时视为校验失败，而不是抛 500
        return False


def legacy_sha256(password: str) -> str:
    """复现旧库 `UserDatabase.db` 的哈希算法：无盐 SHA-256 的十六进制串。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ JWT
def _signing_key(secret: str) -> str:
    """把任意长度的密钥派生成 32 字节的 HMAC key。

    RFC 7518 §3.2 要求 HS256 密钥不短于 32 字节。用户在 config.yaml 里
    很可能写一个短口令，直接用会触发 PyJWT 的 InsecureKeyLengthWarning
    并实质降低安全性。统一派生一次，任意长度配置都安全。
    前缀是用途隔离，避免同一口令被复用到别处。
    """
    return hashlib.sha256(("insect-jwt-v1:" + secret).encode("utf-8")).hexdigest()


def create_access_token(
    subject: str,
    secret: str,
    expires_hours: int = 12,
    algorithm: str = "HS256",
    extra: dict | None = None,
    expires_seconds: int | None = None,
) -> tuple[str, int]:
    """返回 (token, 有效秒数)。subject 用用户 id 的字符串形式。

    `expires_seconds` 给了就覆盖 `expires_hours`。加它是因为 MJPEG 票据只有
    两分钟量级，用小时表达会写成 0（= 立即过期）。既有调用方都不传，行为不变。
    """
    expires_delta = (
        timedelta(seconds=expires_seconds) if expires_seconds is not None
        else timedelta(hours=expires_hours)
    )
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, _signing_key(secret), algorithm=algorithm)
    return token, int(expires_delta.total_seconds())


def decode_token(token: str, secret: str, algorithm: str = "HS256") -> dict:
    """解码并校验 JWT。失败抛出 jwt.PyJWTError 的子类，由调用方转成 401。

    ⚠️ 这是**原始的**解码器：它不关心这枚令牌是干什么用的。凡是要把令牌当成
    "用户登录态"的地方，一律用 `decode_access_token` —— 见那里的注释。
    """
    return jwt.decode(token, _signing_key(secret), algorithms=[algorithm])


def decode_access_token(token: str, secret: str, algorithm: str = "HS256") -> dict:
    """解码一枚**用户登录令牌**：带 `purpose` 声明的令牌一律不认。

    为什么必须有这一步（真实存在过的漏洞）：票据与用户令牌用**同一个密钥、
    同一个算法**签发，`sub` 也都是用户 id。只校验签名的话，一枚 120 秒的 MJPEG 票据
    就是一张 120 秒的**全权账号凭证** —— 而票据按设计就该进 query string
    （访问日志、浏览器历史、Referer），也就是说它**注定会泄漏**。
    `decode_stream_ticket` 那一侧的用途检查只挡住了反方向（用户 JWT 冒充票据），
    这一侧先前是空的：票据能当登录令牌用。
    实测后果不是"看两眼摄像头"，而是拿 `Authorization: Bearer <票据>` 就能
    列出账号下所有设备、解绑、删任务。

    规则写成"不许带 `purpose`"而不是"必须等于 access"：**默认收口**。
    以后再加一种用途受限的令牌（比如只读分享链接），不必记得回来改这里。
    既有用户令牌本来就没有这个字段，所以不破坏任何已签发的登录态。
    """
    payload = decode_token(token, secret, algorithm)
    if payload.get("purpose"):
        raise jwt.InvalidTokenError("这是用途受限的令牌，不能当登录令牌使用")
    return payload


# ------------------------------------------------------------------ 设备令牌
# 令牌前缀不是为了安全，是为了**可辨认**：日志里一眼能看出这是一个设备令牌，
# 而不是某个用户的 JWT（两者长得完全不一样，前缀让这件事更显式）。
_DEVICE_TOKEN_PREFIX = "ipd_"

#: MJPEG 票据的用途标记。`purpose` 进 JWT payload 后，用户 JWT（没有这个字段）
#: 就绝不可能被当成票据用 —— 这是用途隔离的关键一步。
STREAM_TICKET_PURPOSE = "device_stream"


def new_device_token() -> str:
    """生成一枚设备令牌。明文只回给盒子一次，中心只存它的 sha256。"""
    return _DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_device_token(token: str) -> str:
    """设备令牌的存储形式：无盐 sha256 十六进制。

    用 sha256 而非 bcrypt 的理由见本模块开头第 3 条 —— 高熵随机串没有字典空间。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_device_token(token: str | None, token_hash: str | None) -> bool:
    """常量时间比对，避免通过响应耗时逐字节试出哈希。"""
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_device_token(token), token_hash)


def new_pairing_code() -> str:
    """6 位数字配对码。

    定长、纯数字，因为它是**人从树莓派的终端上抄到网页里**的 ——
    混进字母会显著增加抄错率。10^6 的空间配合 5 分钟有效期与绑定接口限流足够，
    真正挡住抢绑的是"已绑定的设备必须先解绑"这条（见 routers/devices.py）。
    """
    return f"{secrets.randbelow(1_000_000):06d}"


def new_pairing_secret() -> str:
    """发起配对时发给盒子的握手凭证。明文只在那一次 `pair/start` 的响应里回给盒子。

    为什么它和设备令牌用同一套哈希与比对：两者都是 `secrets` 生成的 256 位随机串，
    面对的是同一个威胁（有人拿着哈希想还原明文），所以共用一个实现，
    而不是复制一份只改了名字的代码。
    """
    return secrets.token_urlsafe(32)


#: 配对凭证沿用设备令牌的哈希与常量时间比对 —— 见 `new_pairing_secret`。
hash_pairing_secret = hash_device_token
verify_pairing_secret = verify_device_token


def new_stream_ticket(
    user_id: int, device_id: int, secret: str, ttl_s: int = 120
) -> tuple[str, int]:
    """签发 MJPEG 专用票据，返回 (ticket, 有效秒数)。

    **为什么要有票据这种东西**：`<img src="...">` 没法带 `Authorization` 头，
    令牌只能进 query string。而 query 会落进后端访问日志、浏览器历史、Referer，
    把 12 小时的用户 JWT 放进去等于到处撒凭证。所以换成两分钟的、只能看这一台
    设备画面的票据 —— 即使泄漏，影响面也就是"看两眼这台摄像头"。
    """
    return create_access_token(
        str(user_id),
        secret,
        expires_seconds=ttl_s,
        extra={"purpose": STREAM_TICKET_PURPOSE, "device": int(device_id)},
    )


def decode_stream_ticket(ticket: str, device_id: int, secret: str, algorithm: str = "HS256") -> int:
    """校验票据并返回它所属的 user_id。任何不符都抛 `jwt.PyJWTError`。

    三项都要对：签名/有效期（decode 本身）、`purpose`、`device`。
    只校验签名的话，一枚**用户 JWT** 也能当票据使，那就白做了。
    """
    payload = decode_token(ticket, secret, algorithm)
    if payload.get("purpose") != STREAM_TICKET_PURPOSE:
        raise jwt.InvalidTokenError("票据用途不符")
    if payload.get("device") != int(device_id):
        # 特意区分于"用途不符"：这是拿 A 设备的票据去取 B 设备的流
        raise jwt.InvalidTokenError("票据与设备不匹配")
    return int(payload["sub"])


#: 检测产物（标注图 / 原图 / 标注视频）票据的用途标记。
#: 与 `STREAM_TICKET_PURPOSE` 分开：一枚只能看某条任务产物的票据，不该能去看摄像头。
TASK_MEDIA_TICKET_PURPOSE = "task_media"

#: 媒体票据的时间窗口（秒）。见 `new_task_media_ticket`。
TASK_MEDIA_WINDOW_S = 1800


def new_task_media_ticket(
    task_id: int,
    user_id: int,
    secret: str,
    window_s: int = TASK_MEDIA_WINDOW_S,
    algorithm: str = "HS256",
) -> str:
    """签发一枚"只能看这条任务产物"的票据（放进 URL 的 `?mt=`）。

    **为什么需要它**：`/media/outputs` 是静态目录挂载（`StaticFiles`），请求根本不经过
    FastAPI 的依赖注入，所以没有地方挂鉴权；而 `<img>` / `<video>` 又带不了
    `Authorization` 头。真要做到"账号分开"，图片地址本身就得是凭证。

    **为什么没有 `iat`、`exp` 钉在整点窗口上**（这是与 `new_stream_ticket` 唯一的区别）：
    同一对 (任务, 账号) 在同一个窗口里必须算出**同一串**，否则接口每次返回的图片地址
    都不一样，浏览器缓存全部作废 —— 历史页翻一次页就要把 20 张缩略图重下一遍。
    取 `exp = (now // W + 2) * W`，于是有效期落在 [W, 2W)：至少还能用 W 秒，
    至多 2W 秒。代价是同一窗口内所有人拿到的是同一个地址 —— 但那条地址泄漏出去的
    影响面仍然只是"这一条任务的原图与标注图，且最多两小时"。
    """
    now = int(datetime.now(timezone.utc).timestamp())
    expires_at = (now // window_s + 2) * window_s
    payload = {
        "sub": str(user_id),
        "purpose": TASK_MEDIA_TICKET_PURPOSE,
        "task": int(task_id),
        "exp": expires_at,
    }
    return jwt.encode(payload, _signing_key(secret), algorithm=algorithm)


def decode_task_media_ticket(
    ticket: str, task_id: int, secret: str, algorithm: str = "HS256"
) -> int:
    """校验媒体票据并返回它所属的 user_id。任何不符都抛 `jwt.PyJWTError`。

    三项都要对：签名/有效期（decode 本身）、`purpose`、`task`。
    与 `decode_stream_ticket` 同构：只校验签名的话，一枚**用户 JWT** 也能当票据用。
    """
    payload = decode_token(ticket, secret, algorithm)
    if payload.get("purpose") != TASK_MEDIA_TICKET_PURPOSE:
        raise jwt.InvalidTokenError("票据用途不符")
    if payload.get("task") != int(task_id):
        # 拿任务 1 的票据去取任务 2 的产物
        raise jwt.InvalidTokenError("票据与任务不匹配")
    return int(payload["sub"])
