# -*- coding: utf-8 -*-
"""健康检查。

刻意写成「对 app.state 宽容」：推理服务尚未挂载时也要能返回，
便于先验证 Web 骨架，再接入模型。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__
from ..schemas import HealthOut

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    settings = request.app.state.settings
    detector = getattr(request.app.state, "detector", None)

    if detector is None:
        return HealthOut(
            status="ok",
            version=__version__,
            device="unloaded",
            gpu_available=False,
            backend=settings.inference.backend,
            model_loaded=False,
            model_path=None,
            model_names=None,
            fallback_reason=None,
        )

    info = detector.info()
    return HealthOut(
        status="ok",
        version=__version__,
        device=info.get("device", "unknown"),
        gpu_available=bool(info.get("gpu_available", False)),
        # 取后端**实际**的名字（info() 的键是 name），而不是配置里的期望值 ——
        # 否则配了 onnx 却回退到 torch 时会谎报成 onnx，而部署时正是靠这个接口
        # 确认现场跑的到底是哪个后端。
        backend=info.get("name", settings.inference.backend),
        model_loaded=bool(info.get("loaded", False)),
        model_path=info.get("model_path"),
        model_names=info.get("num_classes"),
        fallback_reason=info.get("fallback_reason"),
    )
