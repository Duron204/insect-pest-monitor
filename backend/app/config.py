# -*- coding: utf-8 -*-
"""配置加载。

优先级：环境变量 > config.yaml > 代码内默认值。

刻意不引入 pydantic-settings：本机环境连 pydantic 都需要新装，
用标准库 dataclass + PyYAML 就够，依赖面更小、启动更可控。
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

# backend/app/config.py -> app -> backend -> 项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config.yaml"

logger = logging.getLogger("insect.config")


def _sub(cls: type, raw: Any) -> Any:
    """只保留 dataclass 已知字段，忽略 YAML 里的多余键（便于平滑演进）。"""
    if not isinstance(raw, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    # 生产模式下由后端直接托管前端 dist（同源，避免 CORS 与硬编码端口）
    serve_frontend: bool = True
    reload: bool = False


@dataclass(frozen=True)
class DatabaseConfig:
    url: str = "sqlite:///./backend/data/app.db"


@dataclass(frozen=True)
class InferenceConfig:
    # torch | onnx | ncnn | rknn | hailo（阶段 C 逐步补齐，缺失时自动回退）
    backend: str = "torch"
    default_model: str = "weights/best-yolo11s.pt"
    conf: float = 0.25
    iou: float = 0.5
    imgsz: int = 640
    device: str = "auto"          # auto | cuda:0 | cpu
    max_det: int = 300
    heatmap: bool = False         # 默认关闭：省一次前向，按需开启
    # 启动时预加载默认权重。测试里会关掉，避免每个用例都加载模型。
    preload: bool = True


@dataclass(frozen=True)
class LocalCameraConfig:
    """本机（树莓派）直连摄像头。

    为什么要这一路：现场取图可以直接给边缘盒子（树莓派）插一个摄像头，
    不需要额外硬件；但后端必须有"本地取图"这条路，否则树莓派自己无法产生图像。
    （原先还支持远端采集节点在田里独立拍照上传，那一路已随设备接入层移除。）
    """

    enabled: bool = False
    # auto | picamera2 | opencv | simulated
    #   picamera2  = 树莓派 CSI 排线摄像头（Pi Camera Module），Pi OS 官方栈
    #   opencv     = USB 摄像头 / V4L2 设备
    #   simulated  = 从目录轮读取图片，用于无硬件的开发与自动化测试
    backend: str = "auto"
    # opencv 用：设备索引（0）或设备路径（/dev/video0）；picamera2 忽略此项
    device: str = "0"
    width: int = 1280
    height: int = 720
    # 定时抓拍间隔（分钟）；0 = 不自动抓拍，只在页面手动触发
    interval_min: int = 30
    conf: float = 0.25
    iou: float = 0.5
    # 打开摄像头后先丢弃几帧：自动曝光/白平衡需要时间稳定，
    # 第一帧往往是全黑或严重偏色，直接送检测会得到莫名其妙的漏检。
    warmup_frames: int = 5
    # simulated 用：按文件名排序循环读取该目录下的图片
    simulated_dir: str = "test_media"


@dataclass(frozen=True)
class AgentConfig:
    """盒子（树莓派）作为**边缘代理**外拨到中心后端的配置。

    网络拓扑的关键全在这里：**所有连接都是盒子主动拨出去的**，中心从不反向连盒子。
    于是"局域网先试、以后换公网/4G"就只是改 `center_url` 一个值 ——
    不需要 NAT 穿透、frp、内网映射，也完全不用改前端（浏览器始终只跟中心说话）。

    默认 `enabled=False`：不配置时后端行为与从前逐字节一致。
    启用后会**取代**本机定时抓拍（同一个摄像头只能有一个调度器，
    否则同一帧会被推理两次、每个间隔产生两条任务）。
    """

    enabled: bool = False
    # 局域网阶段填中心机的 IP；以后换公网只改这一项
    center_url: str = "http://127.0.0.1:8000"
    name: str = ""                # 显示名；留空则用 hostname
    hardware_id: str = ""         # 留空则从 /proc/cpuinfo 的 Serial 派生
    stream_enabled: bool = True
    # 推流帧率。默认只传原始 JPEG 不跑推理，2fps 已够"看现场"；
    # 在盒子的设置页开启实时识别后，每一帧都会本机推理并画框，且帧率被限制为
    # min(stream_fps, 2)（见 edge_agent._LIVE_DETECTION_MAX_FPS）。
    stream_fps: int = 2
    stream_quality: int = 70      # 与 stream.jpeg_quality 同一口径
    stream_width: int = 960       # 推流前等比缩到该宽度
    capture_interval_min: int = 0 # 0 = 沿用 local_camera.interval_min
    request_timeout_s: float = 20.0
    reconnect_max_s: float = 60.0
    heartbeat_s: float = 10.0     # last_seen_at 的落库节流间隔（别每帧都写 SD 卡）


@dataclass(frozen=True)
class StreamConfig:
    target_fps: int = 10          # 摄像头抽帧/回传上限
    max_sessions: int = 1         # 单实例：同时只允许 1 个流式会话
    jpeg_quality: int = 70


@dataclass(frozen=True)
class StorageConfig:
    outputs_dir: str = "outputs"
    uploads_dir: str = "uploads"
    keep_uploads: bool = False
    max_upload_mb: int = 500
    max_batch_files: int = 50
    retention_days: int = 30      # 产物/上传保留天数；<=0 表示不清理
    backup_keep: int = 14         # 数据库备份保留份数（0 表示不轮转）


@dataclass(frozen=True)
class TrackingConfig:
    """跟踪去重参数。

    刻意没有"高置信度阈值"（曾有一个 `high_thresh` 但从未被读取）：
    进入跟踪器的检测框已经被检测器的 `conf` 过滤过，再设一道阈值只会
    让低于它的虫子**不被新建轨迹、从而漏计** —— 而本项目的目的是"数清有多少只"，
    不是"只保留最有把握的那几只"。
    """

    enabled: bool = True
    iou_thresh: float = 0.3
    max_age: int = 30             # 连续多少帧未匹配则回收 track


@dataclass(frozen=True)
class AuthConfig:
    jwt_expire_hours: int = 12
    jwt_secret: str = ""          # 留空则自动生成并持久化到 backend/data/.jwt_secret
    algorithm: str = "HS256"


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    # 做成字段而非属性，测试里可指向临时目录，避免污染真实数据目录
    data_dir: Path = PROJECT_ROOT / "backend" / "data"
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    local_camera: LocalCameraConfig = field(default_factory=LocalCameraConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    # 设备上传图片策略：always | on_detect | thumb_only（田间省流量，见计划 §8.5）
    upload_policy: str = "on_detect"

    # ---------- 派生路径 ----------
    @property
    def abs_outputs_dir(self) -> Path:
        return (self.project_root / self.storage.outputs_dir).resolve()

    @property
    def abs_uploads_dir(self) -> Path:
        return (self.project_root / self.storage.uploads_dir).resolve()

    @property
    def sqlite_path(self) -> Path | None:
        """若使用 sqlite，返回数据库文件的绝对路径（供建目录使用）。

        相对路径一律以**项目根**为基准解析，而不是进程 cwd ——
        否则从 backend/ 目录启动 uvicorn 会得到 backend/backend/data/app.db。
        """
        url = self.database.url
        if not url.startswith("sqlite"):
            return None
        raw = url.split("sqlite:///", 1)[-1]
        if not raw or raw == ":memory:":
            return None
        p = Path(raw)
        return p if p.is_absolute() else (self.project_root / p).resolve()

    @property
    def resolved_database_url(self) -> str:
        """把相对 sqlite 路径转成绝对 URL，避免受启动目录影响。"""
        path = self.sqlite_path
        if path is None:
            return self.database.url
        # Windows 需要 posix 风格且保留盘符：sqlite:///D:/x/y.db
        return "sqlite:///" + path.as_posix()

    @property
    def resolved_jwt_secret(self) -> str:
        """JWT 密钥：环境变量 > 配置 > 自动生成并持久化。

        自动持久化是为了让 token 在重启后依然有效，同时避免把密钥写进代码。
        """
        env = os.environ.get("INSECT_JWT_SECRET")
        if env:
            return env
        if self.auth.jwt_secret:
            return self.auth.jwt_secret
        secret_file = self.data_dir / ".jwt_secret"
        if secret_file.exists():
            value = secret_file.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(value, encoding="utf-8")
        return value

    def ensure_dirs(self) -> None:
        """创建运行期目录；SQLite 的父目录必须先存在。"""
        for p in (self.abs_outputs_dir, self.abs_uploads_dir, self.data_dir):
            p.mkdir(parents=True, exist_ok=True)
        db = self.sqlite_path
        if db is not None:
            db.parent.mkdir(parents=True, exist_ok=True)


#: 顶层 key -> 对应的子配置类型。用于校验 YAML 里有没有拼错的键。
SECTION_TYPES: dict[str, type] = {
    "server": ServerConfig,
    "database": DatabaseConfig,
    "inference": InferenceConfig,
    "local_camera": LocalCameraConfig,
    "agent": AgentConfig,
    "stream": StreamConfig,
    "storage": StorageConfig,
    "tracking": TrackingConfig,
    "auth": AuthConfig,
}


def unknown_config_keys(raw: dict[str, Any]) -> list[str]:
    """挑出 YAML 里写了、但代码里根本不认的键。

    **为什么必须有这个**：`_sub()` 为了"平滑演进"会静默丢掉不认识的键。
    代价是拼错一个键（把 `heartbeat_s` 写成 `heartbeat_seconds`）时没有任何报错 ——
    配置看起来改了，实际一直在用默认值。现场排查"心跳把 SD 卡写坏了"这类问题时
    这种静默失效最要命：人对着配置文件改一下午，运行行为纹丝不动。

    返回形如 `["agent.heartbeat_seconds", "storag"]` 的键路径列表。
    """
    problems: list[str] = []
    if not isinstance(raw, dict):
        return problems
    top_known = {f.name for f in fields(Settings)}
    for key, value in raw.items():
        if key not in top_known:
            problems.append(str(key))
            continue
        section = SECTION_TYPES.get(key)
        if section is not None and isinstance(value, dict):
            known = {f.name for f in fields(section)}
            problems.extend(f"{key}.{k}" for k in value if k not in known)
    return sorted(problems)


def load_settings(config_path: Path | str | None = None) -> Settings:
    """读取 config.yaml；文件不存在时全部走默认值（保证开箱可跑）。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded

    # 拼错的键不会报错、只会被静默忽略 —— 这里必须喊出来，
    # 否则"改了配置没生效"会变成一个查无可查的谜。
    for bad in unknown_config_keys(raw):
        logger.warning("config.yaml 里的键 %r 不被识别（拼错了？），已按默认值处理", bad)

    settings = Settings(
        project_root=PROJECT_ROOT,
        server=_sub(ServerConfig, raw.get("server")),
        database=_sub(DatabaseConfig, raw.get("database")),
        inference=_sub(InferenceConfig, raw.get("inference")),
        local_camera=_sub(LocalCameraConfig, raw.get("local_camera")),
        agent=_sub(AgentConfig, raw.get("agent")),
        stream=_sub(StreamConfig, raw.get("stream")),
        storage=_sub(StorageConfig, raw.get("storage")),
        tracking=_sub(TrackingConfig, raw.get("tracking")),
        auth=_sub(AuthConfig, raw.get("auth")),
        upload_policy=str(raw.get("upload_policy", "on_detect")),
    )

    # 环境变量覆盖（容器/现场部署常用）
    if v := os.environ.get("INSECT_HOST"):
        object.__setattr__(settings.server, "host", v)
    if v := os.environ.get("INSECT_PORT"):
        object.__setattr__(settings.server, "port", int(v))
    if v := os.environ.get("INSECT_DB_URL"):
        object.__setattr__(settings.database, "url", v)
    # 推理相关覆盖：边缘部署时用 systemd 的 Environment= 指定后端与权重，
    # 就不必去改现场机器上的 config.yaml（也便于同一镜像跑不同板子）。
    if v := os.environ.get("INSECT_INFERENCE_BACKEND"):
        object.__setattr__(settings.inference, "backend", v)
    if v := os.environ.get("INSECT_INFERENCE_MODEL"):
        object.__setattr__(settings.inference, "default_model", v)
    if v := os.environ.get("INSECT_INFERENCE_DEVICE"):
        object.__setattr__(settings.inference, "device", v)
    # 边缘代理：现场用 systemd 的 Environment= 指定中心地址即可，
    # 不必去改 Pi 上的 config.yaml（同一镜像跑不同盒子时尤其方便）。
    if v := os.environ.get("INSECT_AGENT_ENABLED"):
        object.__setattr__(settings.agent, "enabled", v.strip().lower() in ("1", "true", "yes", "on"))
    if v := os.environ.get("INSECT_AGENT_CENTER_URL"):
        object.__setattr__(settings.agent, "center_url", v)
    if v := os.environ.get("INSECT_AGENT_NAME"):
        object.__setattr__(settings.agent, "name", v)
    return settings
