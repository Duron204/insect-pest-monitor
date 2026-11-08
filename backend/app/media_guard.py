# -*- coding: utf-8 -*-
"""`/media/outputs` 的鉴权闸门：只放行带着有效媒体票据的请求。

**为什么必须是中间件，而不是路由依赖**：`/media/outputs` 是 `StaticFiles` 挂载
（见 `main.py::_mount_media`）。挂载是一个独立的 ASGI 应用，请求**不经过 FastAPI 的
依赖注入** —— 这正是它可以被匿名、按任务号枚举的原因（文件名固定、目录名是自增 id）。
把挂载换成手写路由当然也能鉴权，但 `StaticFiles` 自带的路径穿越防护、Range 支持
（`<video>` 拖动进度条要用）、ETag/304 就得自己再实现一遍，而批量检测的结果还有
`batch/xxx_orig.jpg` 这种嵌套路径。所以这里只做一件事：**放行之前验票**，
通过之后原样交给 `StaticFiles`。

**为什么用纯 ASGI 而不是 `@app.middleware("http")`**：后者（`BaseHTTPMiddleware`）
会把响应重新包一层流，视频的 Range 响应与 TCP 背压都不该多绕一层。这里只看
`scope`，要么直接回 401，要么原样 `await self.app(...)`，开销可忽略。

**为什么验不过回 401 而不是 404**：这不是在隐藏文件是否存在 —— 文件名是固定的
（`annotated.jpg` 就摆在那儿），所以装作不存在没有意义。真实含义是"这张票不对或
过期了"，与 MJPEG 票据接口同一语义。前端收到它应当提示"刷新页面"。

**顺带堵掉的一个真实旁路**：`StaticFiles` 会把路径 `normpath` 之后才判断是否在
根目录内，所以 `/media/outputs/000123/../000124/annotated.jpg` 在它看来是合法的
（就是 000124 的文件）。如果只比对票据里的 `task` 与路径第一段，一枚任务 123 的
票据就能借 `..` 读到任务 124 的图。所以这里在验票**之前**就拒掉含 `.` / `..`
路径段以及反斜杠（Windows 上的分隔符）的请求。
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders, QueryParams
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .security import decode_task_media_ticket
from .services.storage import MEDIA_TICKET_PARAM

#: 受保护的挂载点前缀。`/media/avatars` 刻意不在此列 —— 头像是展示物，
#: 不属于"识别数据"，公开它才不会让每个头像地址都要先请求一次票据。
MEDIA_PREFIX = "/media/outputs/"

_REJECT_DETAIL = "图片链接无效或已过期，请刷新页面重试"


def task_id_from_path(path: str) -> int | None:
    """从 `/media/outputs/000123/annotated.jpg` 取出 123；形状不对返回 None。

    刻意只认"纯数字 + 没有 `.` / `..` / 反斜杠"：见模块头最后一段。
    """
    if "\\" in path:
        return None
    parts = path.split("/")
    # ['', 'media', 'outputs', '<task>', ...]
    if len(parts) < 5 or any(part in (".", "..") for part in parts):
        return None
    # 用 `isdecimal` 而不是 `isdigit`：后者对上标数字（"²"）也返回 True，而
    # `int("²")` 会抛 ValueError —— 那样一个畸形路径会变成 500，不是 401。
    if not parts[3].isdecimal():
        return None
    return int(parts[3])


class TaskMediaGuard:
    """纯 ASGI 中间件：`/media/outputs/*` 必须带 `?mt=<媒体票据>`。"""

    def __init__(self, app: ASGIApp, *, secret: str, algorithm: str = "HS256") -> None:
        self.app = app
        self.secret = secret
        self.algorithm = algorithm

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not scope.get("path", "").startswith(MEDIA_PREFIX):
            await self.app(scope, receive, send)
            return

        task_id = task_id_from_path(scope["path"])
        # Starlette 1.7 的 `QueryParams` 不再接受 scope：直接喂原始 query string。
        # （写成 `QueryParams(scope=scope)` 不会报错，而是把整个 scope 当成
        # 一个查询参数字典序列化 —— 结果永远是 None，闸门就成了"全拒"。）
        ticket = QueryParams(scope.get("query_string", b"")).get(MEDIA_TICKET_PARAM)
        if task_id is None or not ticket:
            await self._reject(scope, receive, send)
            return
        try:
            decode_task_media_ticket(ticket, task_id, self.secret, self.algorithm)
        except Exception:  # noqa: BLE001 - 任何解码/校验失败都等价于"票不对"
            await self._reject(scope, receive, send)
            return

        async def _send_with_policy(message):
            # 票据就在 URL 里。页面跳去外站时浏览器会把完整 URL 放进 Referer，
            # 顺手把这张票送给对方 —— 一条响应头就堵住。
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault(
                    "referrer-policy", "no-referrer"
                )
            await send(message)

        await self.app(scope, receive, _send_with_policy)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": _REJECT_DETAIL},
            status_code=401,
            headers={"referrer-policy": "no-referrer"},
        )
        await response(scope, receive, send)
