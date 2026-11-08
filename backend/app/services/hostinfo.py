# -*- coding: utf-8 -*-
"""描述"后端跑在哪台设备上"，并采集该主机自己的健康指标。

**为什么需要它**：设置页的「本机摄像头」面板控制的是**运行后端的这台设备**的摄像头，
不是浏览器所在电脑的。现场部署时后端就跑在树莓派上（所以那个面板"只控制树莓派"），
但开发机上它会去管开发机的摄像头 —— 界面若不写明是哪台设备，就会让人困惑：
实测反馈是"在 Windows 上看到图像源 opencv、累计抓拍 1，以为这个面板在管自己的电脑"。

主机指标（温度 / 降频 / 磁盘 / 内存）取自 `deploy/field-soak.sh` 那套现场巡检口径 ——
Pi 5 欠压或热降频会让推理耗时翻倍且极难排查，现场第一件事就是确认 `get_throttled` 为 0x0。

**贯穿全文件的一条约定（抄自 field-soak.sh:26-27）**：空值表示"没测到"，**不代表 0**。
没有 vcgencmd、不在 Linux 上、读不到 /proc，一律返回 `None`，绝不填 0 也不抛异常 ——
把 0 当成"正常"会让现场把一个测不到指标的盒子误判为健康。
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "describe_host",
    "collect_host_metrics",
    "decode_throttled",
    "parse_cpu_temp",
    "parse_meminfo",
    "parse_throttled_raw",
    "process_uptime_s",
    "reset_cache",
]

_PI_MODEL_FILE = Path("device-tree") / "model"

# 采集一次要起两个 vcgencmd 子进程（~10-50ms，Pi 降频时更慢）。多个浏览器标签
# 各轮询一次就会成倍放大，而且现场是"省算力"取向（同 config.yaml 里 stream.target_fps
# 那组参数的取舍）。故加一个短 TTL 缓存：指标本身变化很慢，5 秒的陈旧无所谓。
_CACHE_TTL_S = 5.0
_cache: tuple[float, dict[str, Any]] | None = None

# 本进程的启动时刻（单调时钟）。用 uptime 而不是"重启次数"作为主指标：
# 任何平台都拿得到，且更直接地回答"这个服务稳不稳"。
_STARTED_AT = time.monotonic()

# 现场部署时后端由 systemd 拉起（deploy/insect-backend.service）。
# NRestarts 只有在这个形态下才有意义，拿不到就是 None。
_DEFAULT_SERVICE_UNIT = "insect-backend.service"

_SUBPROCESS_TIMEOUT_S = 2.0


def describe_host(proc_root: Path | None = None) -> dict[str, Any]:
    """返回 `{platform, machine, model, is_raspberry_pi}`。

    `proc_root` 只为可测试性存在：真实调用读 `/proc`，测试里传一个临时目录、
    放进伪造的 `device-tree/model`（树莓派上这个文件写着具体型号，例如
    `Raspberry Pi 5 Model B Rev 1.0`）。
    """
    root = proc_root if proc_root is not None else Path("/proc")
    model: str | None = None
    try:
        path = root / _PI_MODEL_FILE
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            model = text.replace("\x00", "").strip() or None
    except OSError:
        model = None

    return {
        "platform": platform.system() or "unknown",
        "machine": platform.machine() or "unknown",
        "model": model,
        # 只有 /proc/device-tree/model 里明确写着 Raspberry Pi 才算数：
        # aarch64 也可能是别的 ARM 板子，不该冒充树莓派
        "is_raspberry_pi": bool(model and "raspberry pi" in model.lower()),
    }


# ---------------------------------------------------------------- vcgencmd
def _run_vcgencmd(args: list[str]) -> str | None:
    """跑一次 vcgencmd，拿不到就返回 None（而不是抛异常）。

    `vcgencmd` 只在树莓派上存在。开发机（Windows/macOS/普通 Linux）上
    `shutil.which` 直接返回 None，连子进程都不会起。
    """
    if shutil.which("vcgencmd") is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - 参数是固定列表，无 shell，无注入面
            ["vcgencmd", *args],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # FileNotFoundError / PermissionError / TimeoutExpired 都归到这里：
        # 现场宁可不显示，也不能让一个卡住的 vcgencmd 拖垮线程池里的请求
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_cpu_temp(raw: str | None) -> float | None:
    """解析 `vcgencmd measure_temp` 的输出，形如 `temp=48.3'C`。"""
    if not raw:
        return None
    _, _, tail = raw.partition("=")
    if not tail:
        return None
    # 只取数字部分：温度后面跟着 'C 或 °C
    number = ""
    for ch in tail.strip():
        if ch.isdigit() or ch in ".-":
            number += ch
        else:
            break
    try:
        return float(number)
    except ValueError:
        return None


def parse_throttled_raw(raw: str | None) -> str | None:
    """从 `vcgencmd get_throttled` 的输出里取出原始值，形如 `0x0`。"""
    if not raw:
        return None
    _, _, tail = raw.partition("=")
    value = (tail or raw).strip()
    return value or None


# get_throttled 的位含义（Raspberry Pi 官方文档）。低 16 位是"当前状态"，
# 高 16 位是"重启以来是否发生过" —— 两者要分开看：正在欠压要立刻处理，
# 曾经欠压说明供电/散热留过隐患。
_THROTTLE_BITS: tuple[tuple[int, str, bool], ...] = (
    (0x1, "欠压", False),
    (0x2, "CPU 频率受限", False),
    (0x4, "正在降频", False),
    (0x8, "软温度限制", False),
    (0x10000, "曾欠压", True),
    (0x20000, "曾频率受限", True),
    (0x40000, "曾降频", True),
    (0x80000, "曾软温度限制", True),
)


def decode_throttled(raw: str | None) -> tuple[bool | None, list[str]]:
    """把 `0x0` / `0x50005` 解成 `(是否正常, 中文标志列表)`。

    解析不了（None 或非十六进制）时返回 `(None, [])` —— 又是"没测到"而非"正常"。
    """
    if not raw:
        return None, []
    try:
        value = int(raw, 16)
    except (TypeError, ValueError):
        return None, []
    flags = [label for bit, label, _ in _THROTTLE_BITS if value & bit]
    return value == 0, flags


# ---------------------------------------------------------------- /proc
def parse_meminfo(raw: str | None) -> tuple[int | None, int | None]:
    """从 `/proc/meminfo` 解析 `(MemAvailable_MB, MemTotal_MB)`。

    用 `MemAvailable` 而不是 `MemFree`：Linux 会把缓存算作已用，`MemFree`
    在正常运行的机器上常年很小，看着像"内存要满了"，是个经典误读。
    """
    if not raw:
        return None, None
    available_kb: int | None = None
    total_kb: int | None = None
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split()
        if not value or not value[0].isdigit():
            continue
        if key.strip() == "MemAvailable":
            available_kb = int(value[0])
        elif key.strip() == "MemTotal":
            total_kb = int(value[0])
    def to_mb(kb: int | None) -> int | None:
        return None if kb is None else round(kb / 1024)

    return to_mb(available_kb), to_mb(total_kb)


def _read_text(path: Path) -> str | None:
    """读一个小文本文件，读不到返回 None（/proc 在非 Linux 上不存在）。"""
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def process_uptime_s(started_at: float | None = None) -> float:
    """本进程已运行秒数。`started_at` 只为可测试性存在。"""
    base = _STARTED_AT if started_at is None else started_at
    return max(0.0, time.monotonic() - base)


# ---------------------------------------------------------------- 磁盘 / systemd
def _disk_usage_mb(path: Path) -> tuple[int | None, int | None, float | None]:
    """`(剩余 MB, 总 MB, 已用百分比)`。任何平台都能算出来。"""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None, None, None
    mb = 1024 * 1024
    free_mb = round(usage.free / mb)
    total_mb = round(usage.total / mb)
    used_percent = round(usage.used / usage.total * 100, 1) if usage.total else None
    return free_mb, total_mb, used_percent


def _service_restarts(unit: str) -> int | None:
    """`systemctl show -p NRestarts`。没有 systemd 就是 None。"""
    if shutil.which("systemctl") is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - 参数是固定列表，无 shell
            ["systemctl", "show", "-p", "NRestarts", "--value", unit],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "").strip()
    # unit 不存在时 systemctl 会给空串或非 0 退出码，两者都当作"没测到"
    if proc.returncode != 0 or not text.isdigit():
        return None
    return int(text)


# ---------------------------------------------------------------- 汇总
def _collect(
    *,
    disk_path: Path,
    proc_root: Path,
    service_unit: str,
    started_at: float | None,
) -> dict[str, Any]:
    """真正干活的那一层（不做缓存）。拆出来是为了让 TTL 缓存只包住它。"""
    temp_c = parse_cpu_temp(_run_vcgencmd(["measure_temp"]))
    throttled = parse_throttled_raw(_run_vcgencmd(["get_throttled"]))
    throttled_ok, throttled_flags = decode_throttled(throttled)

    free_mb, total_mb, used_percent = _disk_usage_mb(disk_path)
    mem = parse_meminfo(_read_text(proc_root / "meminfo"))

    metrics: dict[str, Any] = dict(describe_host(proc_root))
    metrics.update(
        {
            "cpu_temp_c": temp_c,
            "throttled": throttled,
            "throttled_ok": throttled_ok,
            "throttled_flags": throttled_flags,
            "disk_free_mb": free_mb,
            "disk_total_mb": total_mb,
            "disk_used_percent": used_percent,
            "mem_free_mb": mem[0],
            "mem_total_mb": mem[1],
            "uptime_s": round(process_uptime_s(started_at), 1),
            "restarts": _service_restarts(service_unit),
            "sampled_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }
    )
    return metrics


def collect_host_metrics(
    *,
    disk_path: Path | None = None,
    proc_root: Path | None = None,
    service_unit: str = _DEFAULT_SERVICE_UNIT,
    started_at: float | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """采集主机健康指标（含 5 秒 TTL 缓存）。

    `disk_path` 应当传**产物所在的那个卷**（现场就是项目根）：
    填满 SD 卡的是 outputs/uploads，不是 `/`。路由层从 settings 取这个路径，
    本模块不反向依赖配置。
    """
    global _cache
    if not use_cache:
        return _collect(
            disk_path=disk_path or Path.cwd(),
            proc_root=proc_root or Path("/proc"),
            service_unit=service_unit,
            started_at=started_at,
        )

    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1]

    fresh = _collect(
        disk_path=disk_path or Path.cwd(),
        proc_root=proc_root or Path("/proc"),
        service_unit=service_unit,
        started_at=started_at,
    )
    _cache = (now, fresh)
    return fresh


def reset_cache() -> None:
    """清空 TTL 缓存。仅供测试与需要强制刷新的调用方使用。"""
    global _cache
    _cache = None
