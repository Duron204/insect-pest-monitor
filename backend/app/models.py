# -*- coding: utf-8 -*-
"""ORM 模型（SQLAlchemy 2.0 声明式风格）。

时间约定：**全库统一存「无时区的 UTC」**，前端负责转本地时间。
SQLite 不原生支持时区，混用 aware/naive 会导致比较与排序出错，故统一 naive UTC。

字段设计依据：实施计划 §4。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mtime_utc(path: Path) -> datetime:
    """文件的最后修改时间，**换算成 naive UTC**。文件不存在时抛 `OSError`。

    存在的理由：`datetime.fromtimestamp(ts)` 不给 tz 时得到的是**本机时区**的
    时间，而本项目所有 naive datetime 都是 UTC（见 `utcnow`）。两者混着比，
    差值就是这台机器的 UTC 偏移 —— 在 UTC+8 上过期文件会**晚 8 小时**才被清掉，
    在 UTC 以西的机器上则相反，**提前**删掉还没到期的文件。
    盒子上的 `TZ` 完全取决于部署，所以这里必须显式指定 UTC。
    """
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- 常量
class TaskMode:
    IMAGE = "image"
    VIDEO = "video"
    CAMERA = "camera"
    FOLDER = "folder"
    # 边缘盒子（树莓派）本机直连摄像头的定时抓拍。
    LOCAL = "local"
    # 已绑定的树莓派**上报**过来的定时抓拍。
    # 与 LOCAL 的区别在"谁抓的"：LOCAL 是本机后端自己抓的，DEVICE 是别的盒子推来的。
    # （这一路曾经叫 INGEST，随设备接入层一并移除过，现在按账号绑定设备的需求接回来。）
    DEVICE = "device"
    ALL = (IMAGE, VIDEO, CAMERA, FOLDER, LOCAL, DEVICE)


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ALL = (PENDING, RUNNING, DONE, FAILED)


class AlertLevel:
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"
    ALL = (INFO, WARN, CRITICAL)


class DeviceStatus:
    """设备的**生命周期**状态。

    刻意与"在线/离线"分开：这里只回答"这台设备跟某个账号绑没绑上"，
    在线与否是 `last_seen_at` 派生出来的瞬时判断，不落库。
    把两者混成一列，崩溃后就可能永远停在 online 上。
    """

    PENDING = "pending"    # 已在中心发起配对，等用户在网页上输入配对码
    ACTIVE = "active"      # 已绑定到某个账号
    UNBOUND = "unbound"    # 曾经绑定过，已被解绑（历史任务保留）
    ALL = (PENDING, ACTIVE, UNBOUND)


# ---------------------------------------------------------------- 用户
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # bcrypt 哈希；迁移来的老用户可能暂时为 NULL（只靠 legacy_sha256 校验）
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 旧库的无盐 SHA-256（hex64）。登录成功后立即升级为 bcrypt 并清空本列。
    legacy_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    avatar_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    must_reset_password: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<User {self.id} {self.username!r}>"


# ---------------------------------------------------------------- 地块
class Plot(Base):
    """地块。农业语义的核心维度：任务与告警都归属地块。

    `owner_id` 与 `Task.user_id` 同一口径：**有主的只有主人看得见，无主的公共**。
    历史地块迁移过来时是 NULL（= 公共），所以升级一个人都不会丢。
    见 `services/ownership.py` —— 规则只在那一个地方定义。
    """

    __tablename__ = "plots"
    __table_args__ = (Index("ix_plots_owner", "owner_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # 索引在 __table_args__ 里显式命名（不写 index=True，否则同一列会被建两条索引）
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # **全场唯一，刻意不按账号分**：地块是物理世界里的一件东西，"东边那块地"只有一个，
    # 两个账号不该各建一个同名的。代价是拿别人已占用的名字建地块会回 409
    # 「同名地块已存在」—— 对单农场来说这正是想要的语义，不是 bug。
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    crop: Mapped[str | None] = mapped_column(String(64), nullable=True)      # 作物
    area_mu: Mapped[float | None] = mapped_column(Float, nullable=True)      # 面积（亩）
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"<Plot {self.id} {self.name!r}>"


# ---------------------------------------------------------------- 绑定设备
class Device(Base):
    """一台绑到某个账号下的树莓派。

    **数据流方向是"盒子拨出去"**：盒子自己来中心配对、上报、推流，
    中心从不反向连盒子。于是"先用局域网、以后换公网/4G"就只是把
    盒子上的 `agent.center_url` 改一个值 —— 不需要 NAT 穿透或内网映射。

    设备是**账号私有**的：所有面向网页的接口都按 `owner_id` 过滤，
    别人的设备一律 404（不确认它存在）。`Plot` 现在也有 `owner_id`，
    但地块与设备不同 —— 地块是物理世界的实体，"无主的"在所有账号间共用。
    两者的共同规则写在 `services/ownership.py`。
    """

    __tablename__ = "devices"
    __table_args__ = (Index("ix_devices_owner", "owner_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # 稳定硬件标识（树莓派取 /proc/cpuinfo 的 Serial）。重新配对时靠它认出
    # "还是同一台盒子"，从而复用同一行而不是每次重绑都新建一条。
    hardware_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(96), default="")
    model: Mapped[str | None] = mapped_column(String(96), nullable=True)   # Raspberry Pi 5 Model B
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=DeviceStatus.PENDING)

    # 设备令牌**只存 sha256 十六进制**；明文只在配对成功那一刻回给盒子一次。
    # 用无盐 sha256 而非 bcrypt 的理由见 security.py 的模块头：
    # 令牌是高熵随机串，没有字典空间，bcrypt 只会让每次心跳白花 ~100ms。
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    # 待配对时的 6 位数字码与过期时刻。绑定成功即清空（一次性，防重放）。
    pairing_code: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 配对期间只有发起的那台盒子知道的握手凭证（`pair/start` 发、`pair/poll` 交）。
    # **只存 sha256**，与设备令牌同一套算法与理由（见 security.py 的模块头）。
    # 为什么配对码之外还要它：配对码是给人抄的，所以必须短到能抄（10^6），
    # 而 6 位空间在"谁知道 hardware_id"这个前提下挡不住轮询 —— 树莓派的 serial
    # 就明写在 /proc/cpuinfo 里。这个串不给人看，只回答一个二值问题：
    # "你是不是那个发起本次配对的盒子"。
    pairing_secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 配对成功到盒子把令牌取走之间的**短暂**存放。盒子轮询到 paired 后立即清空。
    # 之所以落库而不是放内存：盒子配到一半重启是明确要在支持范围内的场景。
    pending_token_plain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 待交付的令牌**只认**绑定那一刻的那枚凭证（`bind_device` 时从
    # `pairing_secret_hash` 抄一份）。为什么不能只留上面那一列：`pairing_secret_hash`
    # 会被后续的 `pair/start` 换掉，而 `pair/start` 是匿名的 —— 只认"当前那一枚"的话，
    # 知道 serial 的人只要在绑定之后再发一次 `pair/start`，就凭空拿到了
    # "唯一能把待发令牌取走"的凭证（真盒子还没来得及 poll 的话，它就这么被顶了）。
    # 快照下来之后，"令牌只交给把这次配对做到最后的那台盒子"就是**结构上的事实**，
    # 而不是一句注释。与 `pending_token_plain` 同生同灭：只在交付那一刻一起清空。
    pending_token_secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 该设备的默认地块：上报的任务自动落到它。留空表示不归任何地块。
    plot_id: Mapped[int | None] = mapped_column(
        ForeignKey("plots.id", ondelete="SET NULL"), nullable=True
    )

    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    backend: Mapped[str | None] = mapped_column(String(16), nullable=True)   # onnx | torch
    # 心跳。推流时每帧都刷新，但**落库被节流**（见 routers/devices.py 的 _touch_device）——
    # 否则 2fps 推流就是每秒两次写 SD 卡。
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_frame_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_task_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    bound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---- 盒子自报的诊断信息（由 `/ws/agent` 的 status 报文写入）----
    #
    # 这一组回答的是**中心自己算不出来**的问题。中心能算的只有 `online`
    # （最后一次听到它是什么时候），而现场真正卡人的往往是另一句：
    # 「盒子在线，但为什么没画面 / 为什么识别结果一直不来」。
    # 答案只有盒子自己知道：推流开关是不是被关了、摄像头是不是从开机就打不开、
    # 待上报队列是不是积压了。所以让盒子把它自己 `/api/system/agent` 上
    # 那一屏状态原样捎上来，中心只负责存和显示。
    #
    # 全部**可空**：盒子没上报过就是 NULL，前端显示「未上报」，绝不编造 0 / false。
    # （0 帧和"不知道推了几帧"是两件事，混起来就没法排障了。）
    #: 盒子认为自己的推流连接是活的
    agent_streaming: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: 盒子上的 `agent.stream_enabled`：关掉的话在线也必然没画面
    agent_stream_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: 盒子上的实时识别开关（本机推理并画框）
    agent_live_detection: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: 本次进程累计推上去的帧数（盒子重启即归零，看的是"有没有在动"）
    agent_frames_sent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 累计上报成功的识别结果条数
    agent_uploads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 还压在重试队列里没发走的结果条数（>0 说明断过网）
    agent_pending_uploads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 最近一次错误的中文描述。**不会被自动清空** —— 它是"最近一次"，
    #: 显示时带着发生时刻，看时间就知道是不是陈年旧账。
    agent_last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    agent_last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 最后一次收到 status 报文的时刻。与 `last_seen_at` 的区别：后者是
    #: "最后一次听到它"（推流帧也算），这里只认 status 报文 —— 用来判断
    #: 面板上那些数字是刚才的，还是盒子升级之前的旧值。
    agent_status_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 盒子**当前实际在用的画面来源**。取值与 `LocalCameraService` 的图像源名一致：
    #: `picamera2`（CSI 排线摄像头）/ `opencv`（USB 摄像头）/ `simulated`（模拟图，
    #: 即 `test_media/` 里的训练照片）。**这一列是防误导的关键**：auto 后端在真实
    #: 摄像头检测不到时会静默回退到 simulated，网页上看起来"在线、有画面"，
    #: 实则播的是训练图 —— 没有这一列，谁也看不出画面是假的。
    #: NULL 表示盒子还没上报过；显示时务必区分它和"真的在用 simulated"。
    agent_camera_backend: Mapped[str | None] = mapped_column(String(32), nullable=True)

    def __repr__(self) -> str:
        return f"<Device {self.id} {self.name!r} {self.status} owner={self.owner_id}>"


class DeviceIngest(Base):
    """设备上报的幂等台账：记住"这个 seq 已经收过了"。

    **为什么单独一张表，而不是给 tasks 加一个唯一列**：SQLite 的
    `ALTER TABLE ADD COLUMN` 带不了 UNIQUE，唯一约束会逼出一次整表重建；
    而新建一张表 `create_all` 顺手就建好了，风险为零。
    代理在网络抖动后重发同一个 seq 时，靠这张表认出重复、返回既有任务。
    """

    __tablename__ = "device_ingest"
    __table_args__ = (
        UniqueConstraint("device_id", "seq", name="uq_device_ingest_seq"),
        Index("ix_device_ingest_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    #: 这条 seq 落成的那条任务。**任务被删时这条台账要跟着走（CASCADE）**。
    #:
    #: 这里曾经写的是 `SET NULL`，看着更"安全"，实际会造出一个卡死的状态：
    #: 用户在历史页删掉一条任务 → 外键把 task_id 置空 → 而"task_id 为空"
    #: 正是 `_claim_seq` 用来表示"另一个请求正拿着这个 seq 在干活"的信号，
    #: 于是盒子重发这条 seq 会被告知"正在处理中，请稍后再试"，要等满
    #: 一个租约才恢复，而且提示信息还是错的。
    #:
    #: 语义上 CASCADE 也更对：台账回答的是"这个 seq 收过没有"。
    #: 任务既然被删了，这个 seq 就不该再算作"已收过"，盒子重发就该能补回来。
    #: （本项目开启了 `PRAGMA foreign_keys=ON`，见 db.py:43。）
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- 模型注册表
class ModelInfo(Base):
    """已训练/可用的权重注册表。指标来自同目录 results.csv 的解析结果。"""

    __tablename__ = "model_info"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)     # 显示名
    path: Mapped[str] = mapped_column(String(512))                  # 相对项目根
    arch: Mapped[str | None] = mapped_column(String(32), nullable=True)  # yolov8n/yolo11s...
    map50: Mapped[float | None] = mapped_column(Float, nullable=True)
    map50_95: Mapped[float | None] = mapped_column(Float, nullable=True)
    epochs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trained_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="weights")  # weights | runs
    profile: Mapped[str] = mapped_column(String(16), default="pc")       # pc | edge
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    exists: Mapped[bool] = mapped_column(Boolean, default=True)          # 文件是否还在
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"<ModelInfo {self.id} {self.name!r} map50-95={self.map50_95}>"


# ---------------------------------------------------------------- 检测任务
class Task(Base):
    """一次检测任务（人工上传、树莓派本机抓拍，或已绑定设备上报）。"""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_started_at", "started_at"),
        Index("ix_tasks_mode_status", "mode", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 谁的数据。**NULL 不是"丢了归属"，而是"无主 = 公共"** —— 本机摄像头定时抓拍
    #: 写的就是 NULL（`services/local_camera.py`），更早的历史数据也是 NULL。
    #: 可见性规则只在 `services/ownership.py` 定义一次。注意它同时是**删除**的判据：
    #: 看得见 = 能删。
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    plot_id: Mapped[int | None] = mapped_column(
        ForeignKey("plots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 来源设备：人工上传与本机抓拍为 NULL，设备上报的任务指向 devices.id。
    # SET NULL 而非 CASCADE —— 解绑或删设备都不该连带删掉历史检测结果。
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_info.id", ondelete="SET NULL"), nullable=True
    )

    mode: Mapped[str] = mapped_column(String(16))                  # TaskMode
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.PENDING)
    source_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    conf: Mapped[float] = mapped_column(Float, default=0.25)
    iou: Mapped[float] = mapped_column(Float, default=0.5)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    frame_count: Mapped[int] = mapped_column(Integer, default=0)
    # 视频任务的总帧数，用于计算进度；未知时为 NULL
    total_frames: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 框总数 与 跟踪去重后的虫数 —— 两者并列，避免"逐帧求和"式的错误计数
    total_objects: Mapped[int] = mapped_column(Integer, default=0)
    unique_objects: Mapped[int] = mapped_column(Integer, default=0)
    # 每类的框数 {"中文类名": 数量}。单独存一份，是为了让统计接口在
    # detections 明细被抽样/裁剪时依然准确，也免去大表聚合。
    counts_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    result_image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    result_video_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    detections: Mapped[list["Detection"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.mode} {self.status} frames={self.frame_count}>"


# ---------------------------------------------------------------- 检测明细
class Detection(Base):
    """框级明细。frame_index + track_id 是去重计数与历史回放的基础。"""

    __tablename__ = "detections"
    __table_args__ = (
        Index("ix_detections_task_frame", "task_id", "frame_index"),
        Index("ix_detections_class", "class_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    frame_index: Mapped[int] = mapped_column(Integer, default=0)
    class_id: Mapped[int] = mapped_column(Integer, default=0)
    class_name: Mapped[str] = mapped_column(String(128))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    x1: Mapped[int] = mapped_column(Integer, default=0)
    y1: Mapped[int] = mapped_column(Integer, default=0)
    x2: Mapped[int] = mapped_column(Integer, default=0)
    y2: Mapped[int] = mapped_column(Integer, default=0)
    track_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    task: Mapped["Task"] = relationship(back_populates="detections")

    def __repr__(self) -> str:
        return f"<Detection {self.id} task={self.task_id} {self.class_name} @{self.frame_index}>"


# ---------------------------------------------------------------- 告警
class Alert(Base):
    """一条告警。

    `owner_id` 是**从触发它的任务抄过来的**（`evaluate_task` 写 `task.user_id`），
    不是另找的一条归属链：告警属于"谁的数据触发的它"。冗余这一列有两个理由，
    都不是性能：

    1. 列表与 `ack-all` 需要一个能直接写进 `WHERE` 的谓词。走 `task_id` join 到
       `tasks` 也能算，但 `task_id` 是 `SET NULL` 的 —— 用户在历史页删掉那条任务后，
       告警就失去了归属，会**从主人的告警中心里消失**，这显然不对。
    2. 30 分钟去重（`services/alerts.py`）必须按归属分：`plot_id` 常常是 NULL，
       按 `(rule, plot_id)` 去重会让两个账号**互相压掉对方的告警**。
    """

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_ts", "ts"), Index("ix_alerts_owner", "owner_id"))

    id: Mapped[int] = mapped_column(primary_key=True)
    # 索引在 __table_args__ 里显式命名（不写 index=True，否则同一列会被建两条索引）
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    plot_id: Mapped[int | None] = mapped_column(
        ForeignKey("plots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    level: Mapped[str] = mapped_column(String(16), default=AlertLevel.INFO)
    rule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    acked: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------- 虫害知识库
class Species(Base):
    """15 类害虫的农业知识卡：危害、寄主、防治建议。"""

    __tablename__ = "species"

    class_id: Mapped[int] = mapped_column(primary_key=True)
    en_name: Mapped[str] = mapped_column(String(128), unique=True)
    zh_name: Mapped[str] = mapped_column(String(64), index=True)
    host_crops: Mapped[str | None] = mapped_column(Text, nullable=True)
    damage: Mapped[str | None] = mapped_column(Text, nullable=True)
    control: Mapped[str | None] = mapped_column(Text, nullable=True)
    threshold_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    def __repr__(self) -> str:
        return f"<Species {self.class_id} {self.zh_name}>"
