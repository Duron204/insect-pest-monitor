# -*- coding: utf-8 -*-
"""树莓派上报的抓拍结果如何落成一条完整的检测任务。

**这个模块唯一的难点是幂等**，其余都是搬运：解码原图 → 建 `Task` → 交给
`services/capture.py` 的 `persist_frame_task` 烧框落库 → 评估告警。
刻意复用 `persist_frame_task` 而不是另写一套，是为了让"设备上报的任务"和
"本机抓拍的任务"在库里长得一模一样 —— 历史页、看板、报告导出全都不用区分来源。

## 为什么幂等这么要紧

田里的盒子跑在一条会断的网线上。它发完一条抓拍、中心也落库了，但响应在回程丢了，
盒子就会重发。**没有幂等的话，每断一次网就多一条重复的检测记录**，
而重复记录会直接把看板上的虫口数量翻倍 —— 这种错还特别难发现，
因为两条记录看起来都很正常。

幂等靠 `device_ingest` 表上的 `UniqueConstraint(device_id, seq)`：
盒子侧每拍一次把 `seq` 加一，重发的 `seq` 必然撞上已有的那一行。

## 先说抢 seq（并发的那一下）

两个重发请求同时到达时，**唯一约束本身就是闸门**，但光靠它会留下一具尸体：
谁先建好 `Task` 谁就写了一条历史，后到的才发现 seq 被占了 —— 那条已经建出来的
`Task` 得回滚，可 `persist_frame_task` 内部已经 commit 过了，回滚不掉。

所以顺序反过来：**先把 seq 占住，再做重活**。

    claim 台账行（commit）→ 建 Task → persist_frame_task → 回填 ledger.task_id

占住之后每个分支都有明确的去处：
  * `task_id` 非空 → 早就做完了，直接把那一条还回去（`duplicate=True`）
  * `task_id` 为空且在租约内 → 另一个请求正拿着它干活，回 409 让盒子稍后重试
  * `task_id` 为空且租约已过 → 上一个多半是崩在半路，接管重做
  * 任务行被用户在历史页删掉了 → 台账还挂着，允许重做一条

## 明确不做：磁盘 outbox

重发队列在代理进程的内存里（`deque(maxlen=8)`），进程崩掉时队列里那几条会丢。
按 30 分钟一拍的节奏，丢的是"上一次抓拍"，不是"一整天的数据"，
换来的是一份不用管文件锁、不用管磁盘写满的简单实现。
这个取舍在 `edge_agent.py` 的 docstring 里也写了一遍。
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Device, DeviceIngest, Task, TaskMode, TaskStatus, utcnow
from ..schemas import DeviceTaskIn, DeviceTaskIngestOut
from .alerts import evaluate_task
from .capture import persist_frame_task
from .imaging import from_base64_image
from .taskmeta import load_counts

logger = logging.getLogger("insect")

__all__ = ["ingest_device_task"]

#: 一条 seq 被"占住"多久算作废。超时说明占它的那次多半是崩在半路了，允许后来的接管。
#:
#: 它只需要做一件事：**盖住一次真实处理的耗时**（解码 + 烧框约几百毫秒，
#: 大图也在秒级），同时给"进程被 kill 后盒子何时能补发"一个上界。
#: 所以取 60 秒 —— 比处理时间宽两个数量级，又短到不至于让一次崩溃把这条记录
#: 卡住半小时。（这里不需要考虑正常的并发重发：盒子是一条一条发的。）
_CLAIM_LEASE_S = 60.0


def _claim_seq(db: Session, device: Device, seq: int) -> tuple[DeviceIngest, Task | None]:
    """占住 (device_id, seq)。返回 (台账行, 已存在的任务或 None)。

    返回的 `task` 非 None 表示这条 seq 早就处理完了 —— 调用方直接把它还回去，
    绝不能再干一遍。
    """
    row = db.scalars(
        select(DeviceIngest).where(
            DeviceIngest.device_id == device.id, DeviceIngest.seq == seq
        )
    ).first()
    now = utcnow()

    if row is not None:
        if row.task_id is not None:
            existing = db.get(Task, row.task_id)
            if existing is not None:
                return row, existing
            # 台账还挂着，但那条任务已被删掉（用户在历史页删的）。
            # 这时不该把 seq 当成"已处理" —— 那会让盒子永远补不回这条记录。
            logger.info("设备 %s 的 seq=%s 台账仍在但任务已删除，允许重做", device.id, seq)
        elif (
            row.created_at is not None
            and (now - row.created_at).total_seconds() < _CLAIM_LEASE_S
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="同一条记录正在处理中，请稍后重试",
            )
        row.task_id = None
        row.created_at = now
        db.commit()
        return row, None

    row = DeviceIngest(device_id=device.id, seq=seq, created_at=now)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # 并发对手在上一秒抢先插进去了。它的租约是新的，交给它做。
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="同一条记录正在处理中，请稍后重试",
        ) from None
    return row, None


def _decode_image(payload: DeviceTaskIn, settings: Settings):
    """把 base64 原图解码成 BGR 数组；任何问题都变成可读的 4xx，而不是 500。

    注意这个上限是**解析之后**才检查的：请求体到这一步已经进内存了，
    真正的前置防线得靠反向代理的 `client_max_body_size`。这里与
    `routers/detection.py` 对上传文件的处理保持同一口径，
    不为了一个字段单独引入中间件。
    """
    if not payload.image_base64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少原图（image_base64）：没有图像的结果无法在界面上呈现",
        )

    limit_bytes = settings.storage.max_upload_mb * 1024 * 1024
    # base64 约为原始字节的 4/3，先按编码后的长度拦一道，省掉一次无谓的解码
    if len(payload.image_base64) > limit_bytes * 4 // 3 + 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"图像超过 {settings.storage.max_upload_mb} MB",
        )

    try:
        raw_len = len(base64.b64decode(payload.image_base64, validate=False))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="图像 base64 无法解码"
        ) from exc
    if raw_len > limit_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"图像超过 {settings.storage.max_upload_mb} MB",
        )

    frame = from_base64_image(payload.image_base64)
    if frame is None:
        # 解出来是空的：多半是截断的 base64，或者根本不是图像
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="图像内容无法解析（是 JPEG/PNG 吗？）"
        )
    return frame


def _counts_of(task: Task) -> dict[str, int]:
    return load_counts(task)


def ingest_device_task(
    db: Session,
    settings: Settings,
    device: Device,
    payload: DeviceTaskIn,
) -> DeviceTaskIngestOut:
    """把一条设备上报落成检测任务。**设备身份只从令牌来，绝不从请求体来**。"""
    _ledger, existing = _claim_seq(db, device, payload.seq)
    if existing is not None:
        return DeviceTaskIngestOut(
            task_id=existing.id,
            seq=payload.seq,
            duplicate=True,
            total_objects=existing.total_objects or 0,
            unique_objects=existing.unique_objects or 0,
            counts=_counts_of(existing),
        )

    # 解码放在占住 seq 之后：让"图坏了"这类必然重复的失败不至于每次都来回抢一遍。
    # 代价是坏图也占掉一个 seq —— 但盒子对 4xx 的处置是丢弃这一条、seq 继续往前走，
    # 本来就不会重发，所以占掉不影响任何事。
    frame = _decode_image(payload, settings)

    task = Task(
        mode=TaskMode.DEVICE,
        status=TaskStatus.RUNNING,
        source_name=payload.source_name or f"设备 {device.name}",
        # user_id 记绑定账号：设备产生的数据"属于"绑它的人（见 devices.py 的归属边界）
        user_id=device.owner_id,
        device_id=device.id,
        # 目前**唯一**会给盒子抓拍分配地块的路径：设备默认地块
        plot_id=device.plot_id,
        conf=payload.conf,
        iou=payload.iou,
        total_frames=1,
        # 用设备侧的抓拍时刻，而不是中心收到的时刻 ——
        # 网络断了补发时，这两者可能差几十分钟，看板上差一天就完全对不上了
        started_at=payload.captured_at or utcnow(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    counts = persist_frame_task(
        db, settings, task, frame, payload.detections, payload.inference_ms
    )
    triggered = evaluate_task(db, task)

    # 回填台账：这一步之后，这个 seq 才算真正"做完"。
    # 放在 persist 之后是有意的 —— persist 已经 commit 过一次，
    # 这里再来一次，中间的窗口若崩溃，租约到期后会被接管重做（可能多一条），
    # 比"永久少一条且盒子以为发成功了"要好。
    ledger = db.scalars(
        select(DeviceIngest).where(
            DeviceIngest.device_id == device.id, DeviceIngest.seq == payload.seq
        )
    ).first()
    if ledger is not None:
        ledger.task_id = task.id
        db.commit()

    device.last_task_at = utcnow()
    db.commit()

    logger.info(
        "设备 %s（seq=%s）上报落库：task=%s 检出=%d 耗时=%.0fms",
        device.id,
        payload.seq,
        task.id,
        len(payload.detections),
        payload.inference_ms,
    )
    return DeviceTaskIngestOut(
        task_id=task.id,
        seq=payload.seq,
        duplicate=False,
        total_objects=task.total_objects or 0,
        unique_objects=task.unique_objects or 0,
        counts=counts,
        alerts=[a.message for a in triggered],
    )


def prune_ledger(db: Session, settings: Settings, *, now: datetime | None = None) -> int:
    """按 `storage.retention_days` 剪掉过期台账行，返回删除条数。

    台账只需要覆盖"重发窗口"（盒子内存队列最多 8 条、间隔几十分钟），
    留 30 天纯属够用得多。不剪的话它会跟着抓拍次数无限长下去 ——
    这正是 `services/maintenance.py` 存在的意义，所以挂在它那里一起跑。
    """
    retention = int(settings.storage.retention_days)
    if retention <= 0:
        return 0
    cutoff = (now or utcnow()) - timedelta(days=retention)
    rows = db.scalars(select(DeviceIngest).where(DeviceIngest.created_at < cutoff)).all()
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)
