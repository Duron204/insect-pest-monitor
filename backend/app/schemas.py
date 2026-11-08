# -*- coding: utf-8 -*-
"""API 请求/响应模型（pydantic v2）。

本文件是**前后端契约的唯一来源**：前端 `src/api/types.ts` 与之对齐。
新增接口时先在这里定义，再写路由。
"""
from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_serializer

T = TypeVar("T")


class ORMModel(BaseModel):
    """可直接由 SQLAlchemy 对象构造的响应模型。

    附带一个**统一的时间戳序列化约定**：全库时间都以「无时区的 UTC」存储
    （SQLite 不原生支持时区，混用 aware/naive 会让比较与排序出错），
    但 naive 的 ISO 串（`2026-10-09T01:26:59.037816`）交给前端时是有歧义的 ——
    前端只能靠"猜"它是不是 UTC。这里统一补上 `Z`，把隐式约定变成显式契约。
    """

    model_config = ConfigDict(from_attributes=True)

    @model_serializer(mode="wrap")
    def _mark_utc(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if isinstance(data, dict):
            for key, value in data.items():
                if not isinstance(value, str) or len(value) < 19:
                    continue
                # 形如 2026-10-09T01:26:59[.ffffff]，且尾串不含任何时区标记
                if value[4] != "-" or value[10] != "T":
                    continue
                tail = value[19:]
                if tail == "" or not any(ch in tail for ch in ("Z", "+", "-")):
                    data[key] = value + "Z"
        return data


# ---------------------------------------------------------------- 通用
class MessageOut(BaseModel):
    message: str


class HealthOut(BaseModel):
    status: str = "ok"
    version: str
    device: str                      # cuda:0 / cpu
    gpu_available: bool
    backend: str                     # torch / onnx / ...
    model_loaded: bool
    model_path: str | None = None
    model_names: int | None = None   # 类别数
    # 后端回退原因。非空说明配置的后端没起来、实际用了 torch ——
    # 现场部署时这是判断「ONNX 是否真的生效」的关键字段。
    fallback_reason: str | None = None


class Page(BaseModel, Generic[T]):
    """统一分页信封。"""

    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return (self.total + self.page_size - 1) // self.page_size if self.page_size else 0


# ---------------------------------------------------------------- 鉴权
class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    # 注册时限制最少 6 位；登录接口不限制（老账号可能有短口令）
    password: str = Field(min_length=6, max_length=128)
    avatar_path: str | None = Field(default=None, max_length=512)


class UserLogin(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PasswordChange(BaseModel):
    """改口令 / 强制重设（旧库迁移用户首次登录后使用）。"""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class UserOut(ORMModel):
    id: int
    username: str
    avatar_path: str | None = None
    must_reset_password: bool = False
    created_at: datetime | None = None
    last_login_at: datetime | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int                  # 秒
    user: UserOut


class AvatarOut(BaseModel):
    avatar_path: str


# ---------------------------------------------------------------- 检测
class DetectionOut(BaseModel):
    """单个检测框。bbox 顺序为 [x1, y1, x2, y2]，原图坐标系。

    `frame_index` 是**帧号**：历史明细要显示"第几帧"就必须有它，
    视频任务的明细抽样入库后尤其需要它才能定位。
    """

    class_id: int
    class_name: str
    zh_name: str = ""
    score: float
    bbox: list[int]
    track_id: int | None = None
    frame_index: int = 0


class ImageDetectionOut(BaseModel):
    task_id: int
    image_url: str                  # 原图（框由前端 canvas 叠加，不烧进图片）
    annotated_url: str | None = None  # 需要下载/报告时用服务端烧框版本
    width: int
    height: int
    detections: list[DetectionOut]
    counts: dict[str, int]
    frame_total: int
    unique_objects: int
    inference_ms: float


class BatchItemOut(BaseModel):
    filename: str
    image_url: str | None = None
    width: int = 0
    height: int = 0
    detections: list[DetectionOut] = []
    counts: dict[str, int] = {}
    frame_total: int = 0
    inference_ms: float = 0.0
    error: str | None = None


class BatchDetectionOut(BaseModel):
    task_id: int
    total: int
    succeeded: int
    failed: int
    total_objects: int
    counts: dict[str, int]
    items: list[BatchItemOut]
    elapsed_ms: int


class VideoAcceptedOut(BaseModel):
    """视频为后台任务：立即返回 task_id，进度通过 /api/tasks/{id} 或 WS 订阅获取。"""

    task_id: int
    status: str
    source_name: str
    total_frames: int = 0


# ---------------------------------------------------------------- 任务/历史
class TaskOut(ORMModel):
    id: int
    mode: str
    status: str
    source_name: str | None = None
    model_id: int | None = None
    plot_id: int | None = None
    #: 来源设备：人工上传与本机抓拍为 None，绑定的树莓派上报的任务指向 devices.id
    device_id: int | None = None
    conf: float = 0.25
    iou: float = 0.5
    started_at: datetime | None = None
    finished_at: datetime | None = None
    frame_count: int = 0
    total_frames: int | None = None
    duration_ms: int | None = None
    total_objects: int = 0
    unique_objects: int = 0
    result_image_path: str | None = None
    result_video_path: str | None = None
    error: str | None = None


class TaskDetailOut(TaskOut):
    counts: dict[str, int] = {}
    progress: float = 0.0           # 0~100，供前端进度条


# ---------------------------------------------------------------- 模型注册表
class ModelOut(ORMModel):
    id: int
    name: str
    path: str
    arch: str | None = None
    map50: float | None = None
    map50_95: float | None = None
    epochs: int | None = None
    source: str = "weights"
    profile: str = "pc"
    is_default: bool = False
    exists: bool = True
    trained_at: datetime | None = None


class ModelListOut(BaseModel):
    current: str | None = None          # 当前已加载的权重绝对路径
    recommended_id: int | None = None   # 精度最高者，用于提示「建议切换」
    items: list[ModelOut]


# ---------------------------------------------------------------- 物种知识库
class SpeciesOut(ORMModel):
    class_id: int
    en_name: str
    zh_name: str
    host_crops: str | None = None
    damage: str | None = None
    control: str | None = None
    threshold_desc: str | None = None
    image_path: str | None = None


# ---------------------------------------------------------------- 统计
class ClassCountOut(BaseModel):
    zh_name: str
    en_name: str | None = None
    count: int


class DayCountOut(BaseModel):
    date: str
    count: int


class StatsSummaryOut(BaseModel):
    range: str
    total_tasks: int
    total_objects: int
    unique_objects: int
    by_class: list[ClassCountOut]
    by_day: list[DayCountOut]
    by_hour: list[int]             # 长度 24
    plot_count: int = 0


# ---------------------------------------------------------------- 请求体
class ModelSelectIn(BaseModel):
    id: int


class PlotIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    crop: str | None = Field(default=None, max_length=64)
    area_mu: float | None = None
    lat: float | None = None
    lon: float | None = None
    notes: str | None = None


class PlotOut(ORMModel):
    id: int
    name: str
    crop: str | None = None
    area_mu: float | None = None
    lat: float | None = None
    lon: float | None = None
    notes: str | None = None
    created_at: datetime | None = None


class AlertOut(ORMModel):
    id: int
    plot_id: int | None = None
    task_id: int | None = None
    level: str = "info"
    rule: str | None = None
    message: str
    value: float | None = None
    ts: datetime | None = None
    acked: bool = False



# ---------------------------------------------------------------- 本机摄像头（树莓派直连）
class LocalCameraBackendOut(BaseModel):
    """某个图像源在本机是否可用（供页面排障用）。"""

    backend: str
    available: bool
    detail: str = ""


class HostInfoOut(BaseModel):
    """后端运行所在的设备 —— 面板据此说明"本机"到底是哪一台。

    现场部署时后端在树莓派上，所以「本机摄像头」控制的确实是树莓派；
    开发机上则是开发机自己。把这个事实返回给前端，界面才不会含糊。
    """

    platform: str
    machine: str
    model: str | None = None
    is_raspberry_pi: bool = False


class LocalCameraStatusOut(ORMModel):
    """本机摄像头的运行状态。

    继承 ORMModel 是为了复用其"时间戳统一补 Z"的序列化器 ——
    否则这里的 last_capture_at 会是不带时区的裸串，与其它接口不一致，
    前端再按 UTC 解析就会整体偏 8 小时。
    """

    enabled: bool
    running: bool
    backend: str                 # 实际使用的后端（auto 已解析）
    configured_backend: str      # 配置里写的值（可能是 auto）
    device: str
    interval_min: int
    camera_open: bool
    capture_count: int
    last_capture_at: datetime | None = None
    last_task_id: int | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    next_capture_in_s: int | None = None
    available: list[LocalCameraBackendOut] = []
    #: 后端运行所在的设备（现场即树莓派）—— 面板用它说明"本机"是谁
    host: HostInfoOut | None = None


class LocalCameraCaptureOut(BaseModel):
    """一次抓拍的结果摘要。"""

    task_id: int
    trigger: str                 # schedule | manual
    backend: str
    total_objects: int = 0
    unique_objects: int = 0
    counts: dict[str, int] = {}
    inference_ms: float = 0.0
    alerts: list[str] = []


# ---------------------------------------------------------------- 主机状态（设备状态页）
class HostStatusOut(ORMModel):
    """运行后端的这台主机的健康指标（现场＝树莓派）。

    字段口径照抄 `deploy/field-soak.sh` 的现场巡检 CSV，便于两边对照。

    **契约里最要紧的一条**：`| None` 表示**没测到**，不是 0。
    温度/降频/重启次数只在树莓派上拿得到，在开发机上全是 `None`。
    前端必须显示"未测到"，绝不能把 0 当成"正常"—— 否则现场会把一个
    测不到指标的盒子误判为健康（这条约定来自 field-soak.sh:26-27）。

    继承 ORMModel 是为了让 `sampled_at` 也走"补 Z"的统一时间戳序列化。
    """

    # 设备身份（与 HostInfoOut 同源，展开在这里省掉前端一次拼装）
    platform: str
    machine: str
    model: str | None = None
    is_raspberry_pi: bool = False

    #: CPU 温度（℃）。None=没测到（没有 vcgencmd）
    cpu_temp_c: float | None = None
    #: get_throttled 原始值，如 "0x0"；与 field-soak 的 throttled 列逐字一致
    throttled: str | None = None
    #: 由原始值派生：True=正常(0x0)，False=有欠压/降频，None=没测到
    throttled_ok: bool | None = None
    #: 位掩码解码出的中文标志（欠压 / 正在降频 / 曾欠压 …）
    throttled_flags: list[str] = []

    disk_free_mb: int | None = None
    disk_total_mb: int | None = None
    disk_used_percent: float | None = None
    #: 用 MemAvailable 而非 MemFree —— 后者在正常运行的机器上常年很小，易误读
    mem_free_mb: int | None = None
    mem_total_mb: int | None = None

    #: 本**进程**已运行秒数（任何平台都有）—— 回答"这个服务稳不稳"
    uptime_s: float | None = None
    #: systemd 的 NRestarts；只在 systemd 部署形态下有值，否则 None
    restarts: int | None = None

    sampled_at: datetime | None = None


# ---------------------------------------------------------------- 绑定设备
class DevicePairStartIn(BaseModel):
    """盒子发起配对。**无鉴权**（它还没拿到令牌，当然带不了）。"""

    hardware_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=96)
    model: str | None = Field(default=None, max_length=96)
    agent_version: str | None = Field(default=None, max_length=32)
    backend: str | None = Field(default=None, max_length=16)


class DevicePairStartOut(BaseModel):
    pairing_id: int
    #: 6 位数字码，盒子的终端把它印出来，人去网页上输入
    code: str
    #: 握手凭证，**只在这一条响应里出现**。盒子之后每次 `pair/poll` 都要带上它
    #: （`X-Pairing-Secret` 头）—— 否则配对就只认 `hardware_id`，
    #: 而那是可以从 /proc/cpuinfo 读到的公开信息。
    pairing_secret: str = ""
    expires_in: int = 300
    poll_interval_s: int = 2


class DevicePairPollOut(BaseModel):
    """盒子轮询配对结果。

    `device_token` **只在 `status == "paired"` 的那一次响应里出现**，
    盒子取走后中心立即清空明文，之后再也拿不到（重新配对才会发新的）。
    """

    status: str                      # pending | paired | expired | unknown
    device_id: int | None = None
    name: str | None = None
    device_token: str | None = None


class DeviceStreamTicketOut(BaseModel):
    """看实时画面的短期票据。

    网页拿到它之后拼成 `/api/devices/{id}/stream?ticket=...` 丢给 `<img src>`。
    票据**在有效期内可以重复使用**（不是一次性的）—— 因为 `<img>` 在流出错后
    会自动重试同一个 URL，一次性票据会让第一次重连就 401，画面从此永久坏掉。
    """

    ticket: str
    expires_in: int = 120


class DeviceBindIn(BaseModel):
    """用户在网页上输入配对码。"""

    code: str = Field(min_length=1, max_length=16)


class DevicePendingOut(BaseModel):
    """一台正在等待配对的盒子（网页提示用）。

    **绝不下发 `pairing_code`**：码是绑定凭证，等价于"物理接触过这台盒子"的
    证明；要是任何登录用户都能从这里看到码，`bind` 里"拿到码 ≠ 能抢走设备"
    的安全性质就被自己拆掉了。这里只报"有人在敲门"这个事实和盒子的自报信息。
    """

    id: int
    name: str
    model: str | None = None
    agent_version: str | None = None
    backend: str | None = None
    last_seen_at: datetime | None = None
    #: 配对码的过期时刻。前端据此显示"还有几分钟"，过期就不再展示
    pairing_expires_at: datetime


class DeviceUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=96)
    plot_id: int | None = None


class DeviceOut(ORMModel):
    """一台已绑定设备对网页的呈现。

    两个字段是**派生**的、不落库，构造时要显式传：
      * `online` 由 `last_seen_at` 与内存里的最新帧共同推出，绝不写进数据库 ——
        落库的话进程崩溃后会永远停在"在线"。
      * `stream_fps` 是最近一个窗口测出来的实际推流帧率（0 表示没在推）。

    与 `HostStatusOut` 一脉相承的约定：`None` 表示**没测到**，不是 0。
    """

    id: int
    hardware_id: str
    name: str
    model: str | None = None
    status: str
    plot_id: int | None = None
    agent_version: str | None = None
    backend: str | None = None
    last_seen_at: datetime | None = None
    last_frame_at: datetime | None = None
    last_task_at: datetime | None = None
    created_at: datetime | None = None
    bound_at: datetime | None = None

    #: 派生：最近 60 秒内有心跳，且内存里确实有它的帧
    online: bool = False
    #: 派生：最近实测的推流帧率
    stream_fps: float = 0.0

    # ---- 盒子自报的诊断信息（`/ws/agent` 的 status 报文）----
    #
    # 全部**可空，且 None 表示"盒子没报过"**，不是 0、也不是 false。
    # 这一条是刻意与 `HostStatusOut` 保持同一口径的：把"没测到"和"测到是 0"
    # 混成一个值，排障时就会得出"推了 0 帧 → 摄像头坏了"这种凭空的结论，
    # 而真相可能只是这台盒子跑的旧版本、根本不发 status 报文。
    #: 盒子认为自己的推流连接是活的
    agent_streaming: bool | None = None
    #: 盒子上的推流开关。**在线却没画面时第一个要看的就是它**
    agent_stream_enabled: bool | None = None
    #: 盒子上的实时识别开关（推流帧先在本机推理画框）
    agent_live_detection: bool | None = None
    #: 本次进程累计推上来的帧数（盒子重启归零）
    agent_frames_sent: int | None = None
    #: 累计上报成功的识别结果条数
    agent_uploads: int | None = None
    #: 还压在重试队列里的结果条数（>0 说明断过网）
    agent_pending_uploads: int | None = None
    #: 最近一次错误的中文描述。**不会被自动清空**，显示时带着时刻
    agent_last_error: str | None = None
    agent_last_error_at: datetime | None = None
    #: 最后一次收到 status 报文的时刻（判断上面这些数字新不新）
    agent_status_at: datetime | None = None
    #: 盒子当前实际在用的画面来源：`picamera2`/`opencv`/`simulated`。
    #: `simulated` 表示画面来自 `test_media/` 里的训练照片，**不是真实摄像头**，
    #: 网页上要据此给出醒目提示，否则用户会以为看到的是实时画面。
    #: None 表示盒子还没上报过（与上面所有 agent_* 字段一致）。
    agent_camera_backend: str | None = None


# ---------------------------------------------------------------- 设备上报检测结果
class DeviceDetectionIn(BaseModel):
    """设备报上来的一个框（原图坐标系，与 `DetectionOut` 同口径）。"""

    class_id: int
    class_name: str = Field(max_length=64)
    zh_name: str = Field(default="", max_length=64)
    score: float
    bbox: list[int] = Field(min_length=4, max_length=4)
    track_id: int | None = None
    frame_index: int = 1


class DeviceTaskIn(BaseModel):
    """盒子上报的一次定时抓拍。

    **只带原图与框坐标，不带标注图**：烧框由中心自己做
    （`services/capture.py` 的 `persist_frame_task` 已经在做这件事）。
    好处有三 —— 少传一张图、中心不必信任客户端画好的东西、
    标注样式永远由中心一处决定（改样式不用升级田里的盒子）。

    **刻意不收 `counts` / `total_objects` / `unique_objects`**：这三个数中心
    从 `detections` 就能自己算出来，收下来只会多一个可能与中心口径不一致的
    真相来源。设备端算它的那份是为了自己打日志，不是为了让中心存。
    """

    #: 设备侧单调递增的抓拍序号。**幂等的全部依据** —— 重发的判定只看它。
    seq: int = Field(ge=0)
    captured_at: datetime | None = None
    conf: float = 0.25
    iou: float = 0.5
    backend: str | None = Field(default=None, max_length=16)
    source_name: str | None = Field(default=None, max_length=128)
    inference_ms: float = 0.0
    #: 硬上限 500：一帧里几百个框已属异常，不设上限就等于允许任意大的请求体
    detections: list[DeviceDetectionIn] = Field(default_factory=list, max_length=500)
    #: 原图（JPEG/PNG）的 base64。**必填** —— 没有图的结果记录在界面上无法呈现，
    #: 与其静默收下一条看不出所以然的记录，不如让盒子拿到一个说清原因的 400
    image_base64: str | None = None


class DeviceTaskIngestOut(BaseModel):
    """上报结果。

    `duplicate=True` 表示这条 `seq` 之前已经收过，`task_id` 是**那一条**的 id，
    不是新落的 —— 盒子据此可以安全地把本地待发队列里这一条划掉。
    """

    task_id: int
    seq: int
    duplicate: bool = False
    total_objects: int = 0
    unique_objects: int = 0
    counts: dict[str, int] = {}
    alerts: list[str] = []





# ---------------------------------------------------------------- 边缘代理
class AgentLiveDetectionIn(BaseModel):
    """网页上的「实时识别」开关。"""

    enabled: bool


class AgentStatusOut(BaseModel):
    """这台机器上的**边缘代理**（田里的盒子主动外拨给中心）的状态。

    只有 Pi 上才可能有意义：开发机上 `enabled=False`、`paired=False`，
    这不是错误，是"这台机器没在当盒子用"。

    **契约**（与 `HostStatusOut` 同一条）：`| None` 表示**没测到 / 还没发生**，
    不是 0。`last_upload_at` 为 None 是"还没上报过"，不是"上报失败"。

    `pairing_code` 只在**还没绑上**时才有值：它是给人抄去网页上输入的，
    绑上之后中心已经把它清掉了，继续返回只会让人以为还能用。
    暴露它不构成新的泄漏面 —— 同一个码本来就会被打进这台机器的日志里。
    """

    enabled: bool = False
    running: bool = False
    center_url: str = ""
    #: 稳定硬件标识（Pi 的 /proc/cpuinfo Serial）；重新配对靠它认出"还是同一台"
    hardware_id: str = ""
    name: str = ""
    #: 中心那边的设备 id；还没配对成功时为 None
    device_id: int | None = None
    #: 有令牌即为已配对（**派生**，不落库）
    paired: bool = False
    #: 推流连接当前活着（**派生**）
    streaming: bool = False
    stream_enabled: bool = True
    #: 是否在推流画面上本机推理并画框（**落盘偏好**，不是派生状态）
    live_detection: bool = False
    #: 待人去网页输入的 6 位码；已绑上时为空串
    pairing_code: str = ""
    capture_interval_min: int = 0
    #: 已用掉的最大上报序号（幂等靠它单调递增）
    seq: int = 0
    frames_sent: int = 0
    skipped_frames: int = 0
    captures: int = 0
    uploads: int = 0
    pending_uploads: int = 0
    last_upload_at: datetime | None = None
    #: 最近一次错误（中文描述）；None 表示没出过错
    last_error: str | None = None
    last_error_at: datetime | None = None
    uptime_s: float | None = None


# ---------------------------------------------------------------- AI 助手
class AiStatusOut(BaseModel):
    """AI 助手可用性。`configured` 与 `enabled` 分开：
    enabled 是开关（配置里显式关掉），configured 是"key/base_url 齐不齐"。
    前端据此显示"未启用（去 config.yaml）"还是"未配置（去填 key）"。"""

    enabled: bool
    configured: bool
    model: str


class AiChatMessage(BaseModel):
    """一轮历史消息（只收 user / assistant，system 由后端组装）。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class AiChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="本轮问题")
    # 传已有会话 = 追问；None = 新会话（由首问标题自动创建）
    conversation_id: int | None = Field(default=None, description="会话 id；缺省新建")
    history: list[AiChatMessage] = Field(
        default_factory=list,
        max_length=12,
        description="最近对话（旧在前）。服务端仍会按 ai.history_limit 再截断",
    )


class AiCitationOut(BaseModel):
    """一条知识引用。聊天流结束后随 done 事件下发，
    前端渲染成可点击徽标 → 跳知识库卡片。"""

    index: int
    class_id: int
    zh_name: str
    en_name: str
    field: str
    field_label: str
    text: str
    score: float



# ---------------------------------------------------------------- AI 助手·历史与用量
class AiConversationOut(ORMModel):
    """会话列表项。标题取首问截断，消息数用于列表弱化展示。"""

    id: int
    title: str
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class AiMessageOut(ORMModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    citations: list[AiCitationOut] = Field(default_factory=list)
    elapsed_ms: int | None = None
    total_tokens: int | None = None
    created_at: datetime


class AiUsageDayOut(BaseModel):
    day: str
    calls: int
    total_tokens: int


class AiUsageSummaryOut(BaseModel):
    """LLM 用量汇总（按账号）。观测的是"助手到底花了多少"，不做配额限制。"""

    calls: int
    conversations: int
    reports: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_elapsed_ms: int
    by_day: list[AiUsageDayOut]


class AiReportSummaryOut(ORMModel):
    id: int
    range_token: str
    title: str
    total_tokens: int | None = None
    elapsed_ms: int | None = None
    created_at: datetime


class AiReportOut(AiReportSummaryOut):
    content: str
