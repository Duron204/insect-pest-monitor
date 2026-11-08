# -*- coding: utf-8 -*-
"""模型注册表接口。

**解决的实际问题**：项目里有多个权重，而旧代码各处硬编码同一个文件名，
导致 mAP50-95 高 3.8 个点的 yolo11s 完全闲置。这里把「有哪些模型、
各自什么成绩、当前在用哪个、建议切换到哪个」变成可查询的数据。
"""
from __future__ import annotations

from anyio import to_thread
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_current_user, get_db
from ..models import ModelInfo, User
from ..schemas import ModelListOut, ModelOut, ModelSelectIn
from ..services.registry import best_recommendation, ranked, sync
from ..services.storage import UploadTooLarge, read_upload_limited, safe_filename

router = APIRouter(prefix="/models", tags=["models"])


def _detector(request: Request):
    detector = getattr(request.app.state, "detector", None)
    if detector is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="推理服务未初始化"
        )
    return detector


@router.get("", response_model=ModelListOut)
def list_models(
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ModelListOut:
    settings: Settings = request.app.state.settings
    rows = sync(db, settings)

    detector = getattr(request.app.state, "detector", None)
    current_path = None
    if detector is not None and detector.loaded:
        current_path = detector.info().get("model_path")

    recommendation = best_recommendation(rows)
    available = [row for row in ranked(rows) if row.exists]
    return ModelListOut(
        current=current_path,
        recommended_id=recommendation.id if recommendation else None,
        items=[ModelOut.model_validate(row) for row in available],
    )


@router.post("/select", response_model=ModelOut)
async def select_model(
    payload: ModelSelectIn,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ModelOut:
    row = db.get(ModelInfo, payload.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型不存在")
    if not row.exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该权重文件已不存在，请刷新模型列表"
        )

    # .onnx 只能在 onnx 后端下加载（树莓派部署正是这样配置的）。
    # 在 torch 后端上硬加载会抛出 ultralytics 的底层报错，用户看不懂；
    # 这里提前给出可执行的提示。
    settings: Settings = request.app.state.settings
    if row.path.lower().endswith(".onnx") and settings.inference.backend != "onnx":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "该权重是 ONNX 格式，需要把 inference.backend 设为 onnx 才能加载"
                "（树莓派部署即如此；本机开发环境用 torch 时请选 .pt 权重）。"
            ),
        )

    detector = _detector(request)
    try:
        # 加载权重是阻塞操作（读盘 + 预热前向），放线程池避免卡住事件循环
        await to_thread.run_sync(detector.load, row.path)
    except Exception as exc:  # noqa: BLE001 - 需要把失败原因原样告诉用户
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"模型加载失败：{exc}"
        ) from exc
    return ModelOut.model_validate(row)


@router.post("/upload", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
async def upload_model(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ModelOut:
    """登记新的 .pt 权重。

    安全性权衡：`torch.load` 本质上会反序列化 pickle，理论上可执行代码。
    本系统定位是本机单用户（上传者即使用者），且接口要求登录，
    因此接受该风险；**若要开放给多用户/公网，必须改为只允许
    服务端已有的文件，或改用 safetensors/ONNX 等无代码格式。**
    """
    settings: Settings = request.app.state.settings

    if not (file.filename or "").lower().endswith(".pt"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="仅支持 .pt 权重文件"
        )

    # 分块读、超限即停。先 read() 再比大小的话，一个 2GB 的 .pt 会先把内存吃光，
    # 那个 413 反而是 OOM 之前的最后一句话。
    try:
        data = await read_upload_limited(file, settings.storage.max_upload_mb)
    except UploadTooLarge as exc:
        raise HTTPException(
            status_code=413,  # 同上：避免绑定会随 Starlette 版本改名的常量
            detail=str(exc),
        ) from None
    if len(data) < 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="文件过小，不是有效的权重文件"
        )

    target = settings.project_root / "weights" / safe_filename(file.filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    rows = sync(db, settings)
    relative = target.relative_to(settings.project_root).as_posix()
    row = next((item for item in rows if item.path == relative), None)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="权重已保存但登记失败"
        )
    return ModelOut.model_validate(row)
