# -*- coding: utf-8 -*-
"""部署配置模板的校验。

**为什么需要这个测试**：`app.config._sub()` 为了"平滑演进"会**静默忽略未知键**。
这带来一个很隐蔽的部署陷阱 —— 模板里一个拼错的键不会被报错，
而是被丢掉、回落到代码默认值。现场表现就是：

    "config.yaml 明明写了 backend: onnx，实际却在跑 torch"

而且 `/api/health` 的 `fallback_reason` 也查不出来，因为根本没走 onnx 分支
（配置里那个键被忽略了，等于没配后端）。

因此这里逐键核对：模板里的**每个键都必须能被对应的 dataclass 接住**。
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from app.config import SECTION_TYPES, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT_ROOT / "deploy" / "config.pi.yaml"


@pytest.fixture(scope="module")
def template_raw() -> dict:
    if not TEMPLATE.exists():
        pytest.skip(f"配置模板不存在：{TEMPLATE}")
    loaded = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "模板不是合法的 YAML 映射"
    return loaded


def test_template_has_no_unknown_keys(template_raw):
    """模板里的每个键都必须被加载器识别，否则会被静默忽略。"""
    unknown: list[str] = []
    for section, values in template_raw.items():
        if section == "upload_policy":  # 顶层标量，不属于任何 dataclass
            continue
        cls = SECTION_TYPES.get(section)
        if cls is None:
            unknown.append(f"{section}（整段都不认识）")
            continue
        known = {f.name for f in fields(cls)}
        for key in (values or {}):
            if key not in known:
                unknown.append(f"{section}.{key}")
    assert not unknown, (
        "模板里存在加载器不认识的键（会被静默忽略并回落到默认值）：" + ", ".join(unknown)
    )


def test_template_effective_values_are_edge_oriented(template_raw):
    """关键字段必须真的生效 —— 这几个值是 Pi 与开发机的差异所在。"""
    settings = load_settings(TEMPLATE)

    # 推理：走 ONNX，不依赖 torch
    assert settings.inference.backend == "onnx", "边缘模板必须用 onnx 后端"
    assert settings.inference.default_model.endswith(".onnx"), "默认权重应是导出的 onnx"
    assert settings.inference.device == "cpu", "树莓派没有 CUDA"
    assert settings.inference.preload is True, "启动即加载，避免首帧异常"

    # 服务：要让局域网里的手机能访问，且由后端托管前端（同源免 CORS）
    assert settings.server.host == "0.0.0.0"
    assert settings.server.serve_frontend is True

    # 流式：4 核 CPU 带不动 10fps
    assert settings.stream.target_fps <= 10
    assert settings.stream.max_sessions == 1

    # 存储：现场 SD 卡容量有限，必须有保留期
    assert settings.storage.retention_days > 0

    # 本机摄像头：树莓派自己插着摄像头是主要取图方式，模板里应默认启用
    assert settings.local_camera.enabled is True, "边缘模板应启用本机摄像头采集"
    assert settings.local_camera.interval_min >= 1

    # 边缘代理：模板里必须**默认关闭**。这是本段最要紧的一条 ——
    # 打开它会取代本机的定时抓拍，默认打开等于让所有既有部署的行为突然改变。
    assert settings.agent.enabled is False, "边缘代理在模板里必须默认关闭"
    # 但 center_url 要有个像样的占位值，且**绝不能是本机地址**：
    # 盒子与中心同机在部署上没有意义（那样还不如直接开本机网页）。
    assert settings.agent.center_url, "模板里应给出中心地址的占位值"
    assert "127.0.0.1" not in settings.agent.center_url
    assert "localhost" not in settings.agent.center_url


def test_template_does_not_contain_secrets(template_raw):
    """模板里不应出现真实令牌/密钥 —— 它是要进版本库的文件。"""
    auth = template_raw.get("auth") or {}
    assert auth.get("jwt_secret", "") == "", "模板里不应预置 jwt_secret"
    text = TEMPLATE.read_text(encoding="utf-8")
    for marker in ("DEVICE_TOKEN", "Bearer "):
        assert marker not in text, f"模板里不应出现 {marker}"
