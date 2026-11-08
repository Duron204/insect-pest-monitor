# -*- coding: utf-8 -*-
"""产物落盘与上传文件的存放策略。

安全要点：**文件名一律视为不可信输入**。设备与浏览器传来的名字可能带
`..\\..\\` 或绝对路径，因此只取基名并过滤非法字符，绝不直接拼接路径。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ..config import Settings

# 保留中文（现场很可能是中文文件名），其余可疑字符一律替换
_UNSAFE = re.compile(r"[^0-9A-Za-z._\u4e00-\u9fff\u3040-\u30ff-]")


#: 分块读上传内容的块大小。它决定的是**峰值内存**，不是磁盘占用。
_UPLOAD_CHUNK = 1024 * 1024


class UploadTooLarge(ValueError):
    """上传内容超过 `storage.max_upload_mb`。

    继承 `ValueError` 是刻意的：批量上传那条路径本来就按 `except Exception` 逐项
    收集 `str(exc)` 当作用户可见的错误文本，所以它能原样接住这个异常，
    不必为了一个尺寸检查把那条路改成 HTTPException。
    """

    def __init__(self, limit_mb: int) -> None:
        super().__init__(f"文件超过 {limit_mb} MB")
        self.limit_mb = limit_mb


async def read_upload_limited(stream: Any, limit_mb: int) -> bytes:
    """分块读完上传内容，一旦超过 `limit_mb` 立刻中止。

    **为什么不能 `await file.read()` 之后再比大小**：那样限制的只是"允不允许入库"，
    内存早就吃完了。500MB 的 `.pt` 会先分配 500MB 再回 413 —— 在 1~2GB 内存的
    树莓派上，这个 413 本身就是那次 OOM 的遗言。分块读把峰值压在 limit 附近。

    `stream` 只要求有 `await read(n)`，所以 `UploadFile` 与任何流式对象都能用。
    """
    limit = limit_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UploadTooLarge(limit_mb)
        chunks.append(chunk)
    return b"".join(chunks)


def safe_filename(name: str | None, fallback: str = "file") -> str:
    """把任意输入变成安全的单段文件名（不含任何目录成分）。"""
    base = Path(name or "").name            # 去掉目录部分（含 Windows 反斜杠）
    base = base.replace("\\", "/").split("/")[-1]
    cleaned = _UNSAFE.sub("_", base).strip("._")
    return (cleaned or fallback)[:120]


def task_dir(settings: Settings, task_id: int) -> Path:
    """每个任务一个独立产物目录，删除任务时整目录一起删。"""
    directory = settings.abs_outputs_dir / f"{task_id:06d}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def upload_dir(settings: Settings) -> Path:
    directory = settings.abs_uploads_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_upload(settings: Settings, filename: str | None, data: bytes) -> Path:
    """保存原始上传文件，返回绝对路径。"""
    target = upload_dir(settings) / f"{int(time.time() * 1000)}_{safe_filename(filename)}"
    target.write_bytes(data)
    return target


def remove_task_dir(settings: Settings, task_id: int) -> None:
    """删除任务的产物目录（幂等）。"""
    directory = settings.abs_outputs_dir / f"{task_id:06d}"
    if not directory.exists():
        return
    for child in sorted(directory.rglob("*"), reverse=True):
        try:
            child.unlink() if child.is_file() else child.rmdir()
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


def url_for_output(task_id: int, filename: str) -> str:
    """把产物文件映射成前端可直接访问的 URL（/media/outputs 已挂载）。

    **它返回的是一个"裸地址"，还不能直接给浏览器用** —— `/media/outputs` 现在由
    `app/media_guard.py` 守着，必须带 `?mt=<票据>`。要回给前端的地址一律走
    `sign_task_media()`，它会把票据拼上去。库里存的、盘上用的仍然是这个裸地址。
    """
    return f"/media/outputs/{task_id:06d}/{filename}"


#: 媒体票据的查询参数名。`media_guard.py` 读的就是这个 —— 改一处必须改两处。
MEDIA_TICKET_PARAM = "mt"


def sign_task_media(
    path: str | None,
    *,
    task_id: int,
    user_id: int,
    settings: Settings,
) -> str | None:
    """给一条产物的地址拼上 `?mt=<票据>`，让它能被 `<img>` / `<video>` 直接取用。

    只处理 `/media/outputs/` 开头的地址；`None` 与别的地址（绝对 URL 等）原样返回。
    它不是通用地址改写器，只负责"给自家的产物签名"。

    票据是**用途隔离 + 任务隔离**的（见 `security.new_task_media_ticket`）：
    一枚票据只能取那一条任务的产物，拿去当登录令牌也不成立。
    """
    from ..security import new_task_media_ticket

    if not path or not path.startswith("/media/outputs/"):
        return path
    ticket = new_task_media_ticket(
        task_id,
        user_id,
        settings.resolved_jwt_secret,
        algorithm=settings.auth.algorithm,
    )
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{MEDIA_TICKET_PARAM}={ticket}"


def sign_task_media_fields(item: Any, *, task_id: int, user_id: int, settings: Settings) -> Any:
    """把 `TaskOut` / `TaskDetailOut` 上的两个产物地址就地换成签名地址。

    做成"传任意带这两个属性的对象"而不是显式依赖 `schemas.TaskOut`：
    `storage` 是底层模块，不该反过来 import 接口层。代价只是这里没有类型检查，
    所以两个字段名必须与 `schemas.TaskOut` 保持一致（改一处要改两处）。
    """
    item.result_image_path = sign_task_media(
        item.result_image_path, task_id=task_id, user_id=user_id, settings=settings
    )
    item.result_video_path = sign_task_media(
        item.result_video_path, task_id=task_id, user_id=user_id, settings=settings
    )
    return item
