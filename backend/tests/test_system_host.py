# -*- coding: utf-8 -*-
"""主机状态接口（「设备状态」页）的测试。

这一组用例**刻意不依赖树莓派**：CI 跑在 ubuntu-latest、开发机是 Windows，
两边都没有 `vcgencmd`。所以真正要保住的是三件事：

  1. 接口鉴权边界（新接口要鉴权，与不鉴权的 /api/health 形成对照）；
  2. **降级行为**：拿不到指标时返回 None、HTTP 仍然 200 —— 也就是
     field-soak.sh:26-27 那条"空值表示没测到，不代表 0"；
  3. 解析逻辑本身正确（用纯函数单测 + monkeypatch 模拟树莓派的输出）。

凡是"树莓派上才会发生的分支"，一律用 monkeypatch 伪造输入来覆盖，
而不是靠"现场测过"——否则这些分支在 CI 上永远是未执行代码。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import sys

import pytest
from fastapi.testclient import TestClient

from app.services import hostinfo

# ---------------------------------------------------------------- 夹具
@pytest.fixture(autouse=True)
def _clear_cache():
    """TTL 缓存是模块级全局 —— 不隔离会让用例之间互相污染。"""
    hostinfo.reset_cache()
    yield
    hostinfo.reset_cache()


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    creds = {"username": "hoststatus", "password": "hoststatus123"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 接口契约
def test_host_status_requires_auth(client: TestClient):
    """必须鉴权 —— 这正是它没有并进 /api/health 的原因。"""
    assert client.get("/api/system/host").status_code == 401


def test_health_endpoint_stays_public_for_contrast(client: TestClient):
    """对照：/api/health 仍然可以不登录访问（登录页要用）。"""
    assert client.get("/api/health").status_code == 200


def test_host_status_shape_is_complete(client: TestClient, auth_headers):
    """字段集必须齐全 —— 前端按它渲染，悄悄少一个字段会静默崩。"""
    response = client.get("/api/system/host", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    expected = {
        "platform",
        "machine",
        "model",
        "is_raspberry_pi",
        "cpu_temp_c",
        "throttled",
        "throttled_ok",
        "throttled_flags",
        "disk_free_mb",
        "disk_total_mb",
        "disk_used_percent",
        "mem_free_mb",
        "mem_total_mb",
        "uptime_s",
        "restarts",
        "sampled_at",
    }
    assert expected <= set(body)

    # disk / uptime 是任何平台都算得出来的，必须真有值
    assert body["disk_free_mb"] is not None
    assert body["disk_total_mb"] is not None
    assert body["uptime_s"] is not None
    assert body["uptime_s"] >= 0
    assert isinstance(body["throttled_flags"], list)
    # 时间戳按全库约定补 Z，与其它接口一致
    assert body["sampled_at"].endswith("Z")


@pytest.mark.skipif(
    shutil.which("vcgencmd") is not None,
    reason="本机有 vcgencmd（真树莓派），降级断言不适用",
)
@pytest.mark.skipif(
    sys.platform == "linux" and Path("/run/systemd/system").exists(),
    reason="Linux CI 自带 systemd，Pi 字段会读到真实值而非 None（判定按 systemd 可用性）",
)
def test_pi_only_fields_degrade_to_none_not_zero(client: TestClient, auth_headers):
    """没有 vcgencmd 的机器上：Pi 专属指标必须是 None，而不是 0。

    断言"不是 0"是这里的重点 —— 0x0 在树莓派上意味着"完全正常"，
    若把"没测到"也填成 0，现场就会把一个测不到指标的盒子误判为健康。
    """
    body = client.get("/api/system/host", headers=auth_headers).json()
    assert body["cpu_temp_c"] is None
    assert body["throttled"] is None
    assert body["throttled_ok"] is None
    assert body["restarts"] is None
    assert body["throttled_flags"] == []


# ---------------------------------------------------------------- 纯函数解析
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("temp=48.3'C\n", 48.3),
        ("temp=72.0'C\n", 72.0),
        ("temp=0.0'C\n", 0.0),
        ("", None),
        (None, None),
        ("看不懂的输出", None),
    ],
)
def test_parse_cpu_temp(raw: str | None, expected: float | None):
    assert hostinfo.parse_cpu_temp(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("throttled=0x0\n", "0x0"),
        ("throttled=0x50005\n", "0x50005"),
        ("", None),
        (None, None),
    ],
)
def test_parse_throttled_raw(raw: str | None, expected: str | None):
    assert hostinfo.parse_throttled_raw(raw) == expected


def test_decode_throttled_healthy():
    """0x0 是现场唯一可接受的值。"""
    ok, flags = hostinfo.decode_throttled("0x0")
    assert ok is True
    assert flags == []


def test_decode_throttled_under_voltage_now_and_before():
    """0x50005 = 当前欠压 + 当前降频 + 曾欠压 + 曾降频。"""
    ok, flags = hostinfo.decode_throttled("0x50005")
    assert ok is False
    assert "欠压" in flags
    assert "曾欠压" in flags
    assert "正在降频" in flags


def test_decode_throttled_unparsable_is_none_not_healthy():
    """解析不了 → None（没测到），绝不能当成"正常"。"""
    ok, flags = hostinfo.decode_throttled("哈哈")
    assert ok is None
    assert flags == []
    assert hostinfo.decode_throttled(None) == (None, [])


def test_parse_meminfo_uses_available_not_free():
    raw = "MemTotal:       16384000 kB\nMemFree:          200000 kB\nMemAvailable:    8192000 kB\n"
    free_mb, total_mb = hostinfo.parse_meminfo(raw)
    assert total_mb == 16000
    assert free_mb == 8000  # MemAvailable，不是 MemFree(约 195MB)


def test_parse_meminfo_tolerates_missing_available():
    """老内核/裁剪系统可能没有 MemAvailable —— 只能给 None，不能编。"""
    free_mb, total_mb = hostinfo.parse_meminfo("MemTotal:       16384000 kB\n")
    assert total_mb == 16000
    assert free_mb is None
    assert hostinfo.parse_meminfo(None) == (None, None)


def test_process_uptime_uses_injected_origin(monkeypatch):
    """uptime 以注入的起点计算，单调递增且不为负。"""
    import time

    now = time.monotonic()
    assert hostinfo.process_uptime_s(started_at=now - 42.0) >= 42.0
    # 起点在未来（时钟异常）也不能返回负数
    assert hostinfo.process_uptime_s(started_at=now + 1000.0) == 0.0


# ---------------------------------------------------------------- 模拟树莓派
def test_collect_reads_pi_metrics_when_vcgencmd_present(monkeypatch, tmp_path: Path):
    """monkeypatch 出一个"有 vcgencmd 的树莓派"，验证整条采集路径。"""
    outputs = {"measure_temp": "temp=51.2'C\n", "get_throttled": "throttled=0x0\n"}

    def fake_vcgencmd(args: list[str]) -> str | None:
        return outputs.get(args[0])

    monkeypatch.setattr(hostinfo, "_run_vcgencmd", fake_vcgencmd)
    monkeypatch.setattr(hostinfo, "_service_restarts", lambda unit: 3)

    proc = tmp_path / "proc"
    proc.mkdir()
    # 用能整除 1024 的值，避开 round() 的 .5 银行家舍入歧义
    (proc / "meminfo").write_text(
        "MemTotal:       8192000 kB\nMemAvailable:   4096000 kB\n", encoding="utf-8"
    )

    metrics = hostinfo.collect_host_metrics(
        disk_path=tmp_path, proc_root=proc, use_cache=False
    )
    assert metrics["cpu_temp_c"] == 51.2
    assert metrics["throttled"] == "0x0"
    assert metrics["throttled_ok"] is True
    assert metrics["restarts"] == 3
    assert metrics["mem_total_mb"] == 8000
    assert metrics["mem_free_mb"] == 4000
    assert metrics["disk_free_mb"] is not None


def test_run_vcgencmd_survives_missing_binary(monkeypatch):
    """开发机/普通 Linux 上没有 vcgencmd：返回 None，不抛异常。"""
    monkeypatch.setattr(hostinfo.shutil, "which", lambda name: None)
    assert hostinfo._run_vcgencmd(["measure_temp"]) is None


@pytest.mark.parametrize(
    "boom",
    [FileNotFoundError("vcgencmd"), subprocess.TimeoutExpired("vcgencmd", 2.0)],
)
def test_run_vcgencmd_swallows_subprocess_failures(monkeypatch, boom: Exception):
    """子进程缺失/超时必须被吞掉 —— 现场一个卡住的 vcgencmd 不能拖垮请求。"""
    monkeypatch.setattr(hostinfo.shutil, "which", lambda name: "/usr/bin/vcgencmd")

    def raise_boom(*args, **kwargs):
        raise boom

    monkeypatch.setattr(hostinfo.subprocess, "run", raise_boom)
    assert hostinfo._run_vcgencmd(["get_throttled"]) is None


def test_service_restarts_ignores_non_numeric_output(monkeypatch):
    """systemctl 拿不到 unit 时会吐空串或失败 —— 都不是"重启 0 次"。"""
    monkeypatch.setattr(hostinfo.shutil, "which", lambda name: "/usr/bin/systemctl")

    class FakeCompleted:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    monkeypatch.setattr(
        hostinfo.subprocess, "run", lambda *a, **k: FakeCompleted(1, "")
    )
    assert hostinfo._service_restarts("nope.service") is None

    monkeypatch.setattr(
        hostinfo.subprocess, "run", lambda *a, **k: FakeCompleted(0, " 7 \n")
    )
    assert hostinfo._service_restarts("insect-backend.service") == 7


# ---------------------------------------------------------------- TTL 缓存
def test_ttl_cache_collapses_repeated_collection(monkeypatch, tmp_path: Path):
    """缓存窗口内的重复调用只应真正采集一次（省掉 vcgencmd 子进程开销）。"""
    calls = {"n": 0}

    def counting_collect(**kwargs):
        calls["n"] += 1
        return {"marker": calls["n"]}

    monkeypatch.setattr(hostinfo, "_collect", counting_collect)

    first = hostinfo.collect_host_metrics(disk_path=tmp_path)
    second = hostinfo.collect_host_metrics(disk_path=tmp_path)
    assert calls["n"] == 1
    assert first == second == {"marker": 1}

    # 显式绕开缓存（前端"强制刷新"或测试）才应该再采一次
    third = hostinfo.collect_host_metrics(disk_path=tmp_path, use_cache=False)
    assert calls["n"] == 2
    assert third == {"marker": 2}


def test_reset_cache_forces_next_collection(monkeypatch, tmp_path: Path):
    calls = {"n": 0}

    def counting_collect(**kwargs):
        calls["n"] += 1
        return {"marker": calls["n"]}

    monkeypatch.setattr(hostinfo, "_collect", counting_collect)
    hostinfo.collect_host_metrics(disk_path=tmp_path)
    hostinfo.reset_cache()
    hostinfo.collect_host_metrics(disk_path=tmp_path)
    assert calls["n"] == 2
