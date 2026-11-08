# -*- coding: utf-8 -*-
"""auto 后端在"包装了但没有摄像头"时必须回退，而不是永远黑屏。

这是真机上踩出来的缺陷：Pi OS 预装 `python3-picamera2`，而旧实现判定
"picamera2 可用"只看 **包能不能 import**。于是：

  * 盒子上只插了 USB 摄像头（或排线没插好）时，auto 照样选 picamera2；
  * `Picamera2()` 在构造处抛错，`_ensure_open` 没有第二个候选，
    推流每一拍都失败 —— 网页上看到的是"盒子在线，但一直没有画面"；
  * 而 `last_error` 也没被写上，现场没有任何线索可查。

这里用**假的 picamera2 模块**把上述现场搬进测试（不用真实硬件）：
包能导入、但枚举不到摄像头 / 打开即抛错。
"""
from __future__ import annotations

import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.services import local_camera as local_camera_module
from app.services.local_camera import LocalCameraService


def _fake_picamera2(*, cameras: int, open_raises: bool) -> types.ModuleType:
    """造一个 `picamera2` 模块：`cameras` 个摄像头，`open_raises` 时构造即抛错。"""

    class Picamera2:  # noqa: N801 - 模仿第三方类名
        def __init__(self) -> None:
            if open_raises:
                raise RuntimeError("no cameras available")

        @staticmethod
        def global_camera_info() -> list[dict]:
            return [{"Model": "imx708"}] * cameras

        def close(self) -> None: ...

    module = types.ModuleType("picamera2")
    module.Picamera2 = Picamera2
    return module


@pytest.fixture()
def picamera2_installed(monkeypatch):
    """装上一个"包在、但枚举不到摄像头"的 picamera2。"""

    def install(*, cameras: int = 0, open_raises: bool = True):
        fake = _fake_picamera2(cameras=cameras, open_raises=open_raises)
        monkeypatch.setitem(sys.modules, "picamera2", fake)
        return fake

    return install


@pytest.fixture()
def service_factory(settings: Settings, monkeypatch):
    def build(**overrides) -> LocalCameraService:
        overrides.setdefault("backend", "auto")
        cfg = replace(settings.local_camera, enabled=False, **overrides)
        return LocalCameraService(replace(settings, local_camera=cfg), lambda: None, None)

    return build


# ---------------------------------------------------------------- 探测
def test_probe_reports_installed_but_no_camera(picamera2_installed):
    """包装了 ≠ 有摄像头。只判 import 会让 auto 一直选错后端。"""
    picamera2_installed(cameras=0, open_raises=True)

    ok, detail = local_camera_module._probe_picamera2()
    assert ok is False
    assert "未检测到 CSI 摄像头" in detail


def test_probe_detects_present_camera(picamera2_installed):
    picamera2_installed(cameras=1, open_raises=False)

    ok, detail = local_camera_module._probe_picamera2()
    assert ok is True
    assert "imx708" in detail


# ---------------------------------------------------------------- 候选链
def test_candidate_chain_skips_unusable_picamera2(
    picamera2_installed, service_factory
):
    """枚举不到 CSI 时，auto 不该再把 picamera2 排在最前面。"""
    picamera2_installed(cameras=0, open_raises=True)

    service = service_factory()
    order = service.candidate_backends()
    assert "picamera2" not in order, f"没有 CSI 摄像头却仍首选 picamera2：{order}"


def test_candidate_chain_prefers_picamera2_when_present(
    picamera2_installed, service_factory
):
    picamera2_installed(cameras=1, open_raises=False)

    assert service_factory().candidate_backends()[0] == "picamera2"


# ---------------------------------------------------------------- 回退
def test_auto_falls_back_to_next_backend_when_first_cannot_deliver_frames(
    picamera2_installed, service_factory, monkeypatch, tmp_path: Path
):
    """"包在但没摄像头 + OpenCV 也打不开"时，仍要能出图（回退到 simulated）。

    断言的是**真读出一帧**：能打开却读不出帧的源同样要被跳过。
    """
    picamera2_installed(cameras=0, open_raises=True)
    monkeypatch.setattr(
        local_camera_module.OpenCvSource,
        "open",
        lambda self: (_ for _ in ()).throw(RuntimeError("无法打开摄像头设备：0")),
    )

    service = service_factory(simulated_dir="test_media")
    frame, info = service.grab_frame()

    assert frame is not None, "回退之后仍然取不到帧"
    assert info["name"] == "simulated"
    # 回退不能是静默的：页面要能看出画面不是真实摄像头
    assert service.status()["last_error"], "回退原因必须被记录下来"


def test_explicit_backend_does_not_silently_switch(
    picamera2_installed, service_factory, tmp_path: Path
):
    """用户显式选了后端就不能偷偷换一个 —— 选错了要报出真正的原因。"""
    empty = tmp_path / "empty_sim"
    empty.mkdir()
    picamera2_installed(cameras=1, open_raises=False)

    service = service_factory(backend="simulated", simulated_dir=str(empty))
    with pytest.raises(RuntimeError, match="没有图片"):
        service.grab_frame()


def test_no_usable_source_reports_every_attempt(
    picamera2_installed, service_factory, monkeypatch, tmp_path: Path
):
    """全都不可用时，错误里要列出每个候选的原因，而不是一句"取帧失败"。"""
    empty = tmp_path / "empty_sim2"
    empty.mkdir()
    picamera2_installed(cameras=0, open_raises=True)
    monkeypatch.setattr(
        local_camera_module.OpenCvSource,
        "open",
        lambda self: (_ for _ in ()).throw(RuntimeError("无法打开摄像头设备：0")),
    )

    service = service_factory(simulated_dir=str(empty))
    with pytest.raises(RuntimeError) as excinfo:
        service.grab_frame()

    message = str(excinfo.value)
    assert "opencv" in message and "simulated" in message, message
