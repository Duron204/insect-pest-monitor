# -*- coding: utf-8 -*-
"""本机（树莓派）直连摄像头接口。

镜头由边缘盒子自己插、盒子自己定时抓拍，抓拍结果与人工上传的任务一样
进入历史、看板与报告，用 `mode=local` 区分。

（原先还有一条远端采集节点上报的路 `/api/ingest/*`，已随设备接入层移除。）
"""
from __future__ import annotations

from functools import partial

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import get_current_user
from ..models import User
from ..schemas import LocalCameraBackendOut, LocalCameraCaptureOut, LocalCameraStatusOut
from ..services.local_camera import LocalCameraService

router = APIRouter(prefix="/local-camera", tags=["local-camera"])


def _service(request: Request) -> LocalCameraService:
    service = getattr(request.app.state, "local_camera", None)
    if service is None:
        # 理论上 create_app 一定会挂上；真出现说明装配漏了，明确报错而不是 500 空栈
        raise HTTPException(status_code=503, detail="本机摄像头服务未初始化")
    return service


@router.get("/status", response_model=LocalCameraStatusOut)
def local_camera_status(
    request: Request,
    _: User = Depends(get_current_user),
) -> LocalCameraStatusOut:
    """当前状态：后端、是否在定时抓拍、上次抓拍/错误、各图像源可用性。"""
    return LocalCameraStatusOut.model_validate(_service(request).status())


@router.get("/backends", response_model=list[LocalCameraBackendOut])
def local_camera_backends(
    request: Request,
    _: User = Depends(get_current_user),
) -> list[LocalCameraBackendOut]:
    """本机可用的图像源（含不可用的原因，便于现场排障）。"""
    entries = _service(request).available_backends()
    return [LocalCameraBackendOut.model_validate(entry) for entry in entries]


@router.post("/capture", response_model=LocalCameraCaptureOut)
async def local_camera_capture(
    request: Request,
    _: User = Depends(get_current_user),
) -> LocalCameraCaptureOut:
    """立即抓一拍（不等定时）。

    放线程池里执行：打开 picamera2 要 1~2 秒、推理是重活，
    直接在事件循环里跑会把整个后端卡住（同一时刻的页面请求都会转圈）。
    """
    service = _service(request)
    try:
        result = await to_thread.run_sync(partial(service.capture_once, trigger="manual"))
    except Exception as exc:  # noqa: BLE001 - 把原因原样告诉页面，而不是 500 空栈
        raise HTTPException(status_code=500, detail=f"抓拍失败：{exc}") from exc
    return LocalCameraCaptureOut.model_validate(result)


@router.post("/start", response_model=LocalCameraStatusOut)
def local_camera_start(
    request: Request,
    _: User = Depends(get_current_user),
) -> LocalCameraStatusOut:
    """启动定时抓拍。间隔为 0 时不会启动线程（只允许手动抓拍）。"""
    return LocalCameraStatusOut.model_validate(_service(request).start())


@router.post("/stop", response_model=LocalCameraStatusOut)
def local_camera_stop(
    request: Request,
    _: User = Depends(get_current_user),
) -> LocalCameraStatusOut:
    """停止定时抓拍并释放摄像头（USB 摄像头的指示灯会随之熄灭）。"""
    return LocalCameraStatusOut.model_validate(_service(request).stop())
