# -*- coding: utf-8 -*-
"""FastAPI 应用入口。

启动方式（三种，任选）：
    1) 双击项目根 start_backend.bat
    2) cd backend && python -m app.main
    3) cd backend && python -m uvicorn app.main:create_app --factory --port 8000

刻意**不在模块级创建 app** —— 否则 `import app.main` 就会建库、写文件，
测试与脚本导入时会带上副作用。要做 ASGI 目标请用 `--factory`。
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from . import __version__
from .config import Settings, load_settings
from .db import add_missing_columns, create_all, init_engine, session_factory
from .media_guard import TaskMediaGuard
from .routers import alerts as alerts_router
from .routers import ai as ai_router
from .routers import auth as auth_router
from .routers import detection as detection_router
from .routers import devices as devices_router
from .routers import health as health_router
from .routers import local_camera as local_camera_router
from .routers import models as models_router
from .routers import plots as plots_router
from .routers import species as species_router
from .routers import stats as stats_router
from .routers import system as system_router
from .routers import tasks as tasks_router
from .services.detector import DetectorService
from .services.edge_agent import EdgeAgentService
from .services.frame_hub import FrameHub
from .services.local_camera import LocalCameraService
from .services.maintenance import purge_artifacts
from .services.video import reset_stale_tasks
from .ws import agent as ws_agent
from .ws import detection as ws_detection

logger = logging.getLogger("insect")

# 开发期前端跑在 Vite 端口上才需要 CORS；生产由后端同源托管，不走 CORS
_DEV_CORS_ORIGINS = [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]

# 这些前缀不参与 SPA 回退：未命中时应返回 JSON 404，而不是把 index.html 当接口返回
_API_PREFIXES = ("api/", "media/", "ws/", "docs", "redoc", "openapi.json")

# 设备画面清扫：多久扫一次、多久没更新算过期。
# 过期阈值取得比 ONLINE_WINDOW_S（60 秒）短，是为了让"画面已经不新了"
# 比"设备算离线了"更早发生 —— 观众不该在徽标还绿着的时候盯着一个冻住的画面。
_FRAME_SWEEP_INTERVAL_S = 15.0
_FRAME_MAX_AGE_S = 30.0


async def _sweep_stale_frames(app: FastAPI) -> None:
    """定期丢掉太久没更新的设备画面。

    正常情况下 `/ws/agent` 断开时就会 `hub.forget`，但**半开连接**
    （网线拔了、对端直接断电）不会触发断开事件，那一帧就会一直留在内存里，
    被当成"当前画面"发给后来的观众。这个清扫是那种情况下的兜底：
    **只丢内存里那一帧，数据库里的设备行不动** —— 网络断了而已，
    绑定关系、历史记录都不该受影响。
    """
    hub = app.state.frame_hub
    while True:
        await asyncio.sleep(_FRAME_SWEEP_INTERVAL_S)
        try:
            dropped = hub.evict_stale(_FRAME_MAX_AGE_S)
        except Exception as exc:  # noqa: BLE001 - 清扫失败不该拖垮这个循环
            logger.warning("清理过期画面失败：%s", exc)
            continue
        if dropped:
            logger.info("已清理 %d 台设备的过期画面：%s", len(dropped), dropped)


def _build_route_table(app: FastAPI) -> list[tuple[re.Pattern[str], set[str]]]:
    """从 OpenAPI 描述里建一张「路径正则 -> 允许的方法」表。

    为什么不用 `app.router.routes` 逐个匹配：本项目实测的
    Starlette 1.7 / FastAPI 0.143 里，`include_router` 之后
    `app.router.routes` 中**每个子路由只对应一个不透明的条目**
    （`path` 与 `path_regex` 都是 None），拿不到真实路径与方法，
    因此那条路走不通。OpenAPI 描述是稳定的公开契约，与内部实现解耦。
    """
    table: list[tuple[re.Pattern[str], set[str]]] = []
    try:
        spec = app.openapi()
    except Exception:  # noqa: BLE001 - 取不到描述时退化为"路径不存在"判断
        return table
    for path, operations in (spec.get("paths") or {}).items():
        pattern = re.compile("^" + re.sub(r"\{[^/}]+\}", "[^/]+", path) + "$")
        methods = {
            name.upper()
            for name in operations
            if name.lower() in ("get", "post", "put", "patch", "delete")
        }
        table.append((pattern, methods))
    return table


def _lookup_route(app: FastAPI, path: str) -> tuple[bool, set[str]]:
    """返回 (该路径是否存在于任何方法, 该路径允许的方法集合)。结果缓存在 app.state。"""
    table = getattr(app.state, "route_table", None)
    if table is None:
        table = _build_route_table(app)
        app.state.route_table = table
    for pattern, methods in table:
        if pattern.match(path):
            return True, methods
    return False, set()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()

    # 启动时就把 JWT 密钥落定：resolved_jwt_secret 是惰性的（首个签发/校验时才生成），
    # 若等到并发请求时才创建，理论上两个请求可能各自生成一份。
    # 这里提前触发一次，让 .jwt_secret 的出现时机可预期。
    _ = settings.resolved_jwt_secret

    # 建库（幂等）。URL 已解析为绝对路径，不受启动目录影响。
    init_engine(settings.resolved_database_url)
    create_all()
    # 给**已经存在**的库补上模型里新加的可空列。缺列会让整行查询报
    # `no such column`，设备列表整个 500 —— 而这套系统是靠"拷文件夹 + 重启"
    # 部署的，不能指望有人先跑迁移脚本。见 db.add_missing_columns 的说明。
    add_missing_columns()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """启动/收尾本机摄像头定时抓拍。

        只在配置里启用时才启动；未启用（或没有摄像头）时服务照常运行，
        只是这一路不产生图像 —— 不该因为没插摄像头就让整个后端起不来。
        """
        service = getattr(application.state, "local_camera", None)
        agent = getattr(application.state, "agent", None)
        # 启用边缘代理时**不启动**本机定时抓拍：摄像头只有一个，
        # 两个调度器各抓各的会让同一帧推理两次、每个间隔产出两条任务。
        # 代理的抓拍循环就是那唯一的调度器，只是把结果发给了中心而不是本机。
        local_scheduler_active = settings.local_camera.enabled and not (
            agent is not None and agent.enabled
        )
        if service is not None and local_scheduler_active:
            try:
                service.start()
            except Exception as exc:  # noqa: BLE001 - 摄像头问题不能拖垮服务启动
                logger.warning("本机摄像头定时抓拍启动失败：%s", exc)

        if agent is not None and agent.enabled:
            try:
                agent.start()
            except Exception as exc:  # noqa: BLE001 - 代理起不来不能拖垮服务
                logger.warning("边缘代理启动失败：%s", exc)

        # 设备画面的过期清扫（半开连接的兜底，见 _sweep_stale_frames）
        sweeper = asyncio.create_task(_sweep_stale_frames(application))

        # 启动时兜底清理一次过期产物与上传文件。
        # 单机部署不一定装了定时器，这里做一次代价很小的兜底；
        # 任何失败都只记日志 —— 清理不该拖垮服务启动。
        try:
            session = session_factory()()
            try:
                summary = purge_artifacts(settings, session)
            finally:
                session.close()
            if summary.get("enabled") and (
                summary["tasks"] or summary["orphan_dirs"] or summary["uploads"]
            ):
                logger.info(
                    "启动清理完成：任务产物 %s 个、孤儿目录 %s 个、上传 %s 个，释放 %.1f MB",
                    summary["tasks"],
                    summary["orphan_dirs"],
                    summary["uploads"],
                    summary["bytes"] / 1e6,
                )
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响启动
            logger.warning("启动清理失败（已忽略）：%s", exc)

        try:
            yield
        finally:
            sweeper.cancel()
            try:
                await sweeper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关停时不抛
                pass
            if agent is not None:
                try:
                    agent.shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("边缘代理收尾失败：%s", exc)
            if service is not None:
                try:
                    service.shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("本机摄像头收尾失败：%s", exc)

    app = FastAPI(
        title="病虫害智能监测系统 API",
        version=__version__,
        lifespan=lifespan,
        description="MCU 采集 → 边缘推理 → 看板；三层共用同一套接口契约。",
    )
    app.state.settings = settings
    app.state.session_factory = session_factory()
    app.state.bg_jobs = set()          # 持有后台任务引用，防止被 GC 回收
    app.state.detector = DetectorService(settings)
    # 本机（树莓派）直连摄像头的定时抓拍。构造它不打开摄像头 ——
    # 只在真正抓拍时惰性打开，避免"没插摄像头就起不来服务"。
    app.state.local_camera = LocalCameraService(
        settings, app.state.session_factory, app.state.detector
    )
    # 边缘代理（只在 `agent.enabled` 时才真正启动，见 lifespan）。
    # 总是构造：`GET /api/system/agent` 未启用时也要能看一眼配置与硬件标识。
    # 构造它不会连网、不会开摄像头 —— 那些都发生在线程里。
    app.state.agent = EdgeAgentService(
        settings, app.state.detector, app.state.local_camera
    )
    # 流式会话配额：同时只允许 N 个（见 ws/detection.py 的说明）
    app.state.active_streams = 0
    app.state.stream_lock = asyncio.Lock()
    # 已绑定设备推上来的**最新一帧**，供网页以 MJPEG 观看（见 services/frame_hub.py）。
    # 纯内存、只留一帧：这是"看现场直播"，不是录像。
    # 必须在注册路由之前构造 —— routers/devices.py 与 ws/agent.py 都要读它。
    app.state.frame_hub = FrameHub()

    # 上次异常退出留下的 running/pending 任务标记为失败，否则前端进度条永远转圈
    try:
        reset_stale_tasks(app.state.session_factory)
    except Exception as exc:  # noqa: BLE001 - 清理失败不应阻止服务启动
        logger.warning("清理残留任务失败：%s", exc)

    if settings.inference.preload:
        try:
            app.state.detector.load()
            logger.info("已预加载模型：%s", settings.inference.default_model)
        except Exception as exc:  # noqa: BLE001 - 权重缺失也要能启动，便于先看接口
            logger.warning("预加载模型失败（可稍后在模型页手动选择）：%s", exc)

    # `/media/outputs` 的媒体票据闸门。**必须加在 CORSMiddleware 之前**：
    # `add_middleware` 是往栈顶插，后加的在外层 —— 这样 CORS 仍包在最外面，
    # 闸门回的 401 也能带上 CORS 头。否则浏览器只报 "CORS error"，
    # 真正的原因（票据过期）被藏起来，现场很难查。
    app.add_middleware(
        TaskMediaGuard,
        secret=settings.resolved_jwt_secret,
        algorithm=settings.auth.algorithm,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 压缩放在最外层（最后 add 的在外层）：单端口模式下前端那 1.5MB 产物
    # 由 FastAPI 自己吐，不压缩的话 Pi 的百兆口首屏要多传几百 KB。
    # 阈值 1KB：更小的响应压缩收益不抵 CPU（Pi 的 CPU 留给推理）。
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.exception_handler(405)
    async def _method_not_allowed(request: Request, exc: Exception):  # noqa: ARG001
        """把 405 的默认文案换成可执行的提示，并纠正"路径其实不存在"的情况。

        背景：SPA 回退路由只声明了 GET，因此**任何方法**打到不存在的路径时，
        Starlette 会认为"路径匹配了但方法不允许"，返回
        405 {"detail":"Method Not Allowed"}。这个提示会把人引向
        "方法用错了"的错误方向 —— 真机上就为此排查了很久
        （实际原因是前端某个请求漏了 /api 前缀）。

        这里用路由表本身判断路径是否真实存在：
        - 存在但方法不对 -> 405，并说明是哪个方法不支持
        - 路径根本不存在 -> 404，并提示去检查前缀/路径
        """
        path = request.url.path
        exists, allowed = _lookup_route(request.app, path)
        if exists:
            allow_text = "/".join(sorted(allowed)) or "其它方法"
            detail = f"该路径不支持 {request.method} 方法：{path}（该路径允许：{allow_text}）"
        else:
            detail = (
                f"接口不存在：{path}（请检查请求路径是否写错，"
                "例如前端漏了 /api 前缀、或多写了结尾斜杠）"
            )
        return JSONResponse(status_code=405 if exists else 404, content={"detail": detail})

    app.include_router(health_router.router, prefix="/api")
    app.include_router(auth_router.router, prefix="/api")
    app.include_router(models_router.router, prefix="/api")
    app.include_router(species_router.router, prefix="/api")
    app.include_router(detection_router.router, prefix="/api")
    app.include_router(tasks_router.router, prefix="/api")
    app.include_router(stats_router.router, prefix="/api")
    # 地块 / 告警
    app.include_router(plots_router.router, prefix="/api")
    app.include_router(local_camera_router.router, prefix="/api")
    app.include_router(alerts_router.router, prefix="/api")
    app.include_router(ai_router.router, prefix="/api")
    # 绑定设备（账号下的树莓派）：配对码绑定、结果查询、实时画面。
    # 里面既有面向盒子的匿名接口，也有面向用户的登录接口，见该模块 docstring。
    app.include_router(devices_router.router, prefix="/api")
    # 主机状态（「设备状态」页）—— 与不鉴权的 /api/health 分开，见 routers/system.py
    app.include_router(system_router.router, prefix="/api")

    # WebSocket 不挂 /api 前缀：路径自带 /ws/detection、/ws/agent
    app.include_router(ws_detection.router)
    app.include_router(ws_agent.router)

    _mount_media(app, settings)
    _mount_frontend(app, settings)
    return app


def _mount_media(app: FastAPI, settings: Settings) -> None:
    """把头像与检测产物以静态文件暴露给前端。

    两者待遇不同：头像是展示物，公开；检测产物是账号数据，由
    `media_guard.TaskMediaGuard` 在 ASGI 层验 `?mt=` 票据
    （挂载不经过 FastAPI 依赖，所以闸门只能做在中间件里 —— 见该模块的说明）。
    """
    avatars = settings.data_dir / "avatars"
    avatars.mkdir(parents=True, exist_ok=True)
    app.mount("/media/avatars", StaticFiles(directory=avatars), name="avatars")
    app.mount("/media/outputs", StaticFiles(directory=settings.abs_outputs_dir), name="outputs")


def _within(dist: Path, full_path: str) -> Path | None:
    """把 URL 路径映射成 dist 内的真实路径；越界一律返回 None。

    **必须校验，不能直接 `dist / full_path`**：`full_path` 整段来自 URL，
    `dist / "../../config.yaml"` 会逃出 dist，于是未授权就能读任意文件
    （实测 `GET /%2e%2e/%2e%2e/config.yaml` 曾返回 200 与真实内容）。
    Windows 上还有第二种逃逸：`dist / "C:/Windows/win.ini"` 会因为右操作数是
    绝对路径而**整体替换**掉左操作数，连 `..` 都不用。故统一走 resolve + 前缀校验，
    顺带把指向 dist 之外的符号链接也挡掉。
    """
    if not full_path:
        return None
    try:
        candidate = (dist / full_path).resolve()
    except (OSError, ValueError):
        # 非法路径（超长、含 NUL 等）：当作不存在，不要漏成 500
        return None
    return candidate if candidate.is_relative_to(dist) else None


class _HashedAssets(StaticFiles):
    """给带内容哈希的构建产物加长缓存头。

    Vite 产出的文件名形如 `DevicesView-gVxcuZDj.js` —— **内容变了文件名就变**，
    所以可以放心缓存一年（`immutable`），浏览器第二次进页面不再请求这些文件。
    `index.html` 不能用这条规则（它文件名不变、内容会变），所以它走 no-cache，
    见下面的 `_spa_headers()`。
    """

    def file_response(  # type: ignore[override]
        self, full_path, stat_result, scope, status_code: int = 200
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        return response


def _spa_headers() -> dict[str, str]:
    """首页/深链接：每次都要问一次。缓存住就会出现"发了新版但页面还是旧的"。"""
    return {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """生产模式：同源托管前端构建产物，并做 SPA 回退。"""
    dist = settings.project_root / "vue-frontend" / "dist"
    if not (settings.server.serve_frontend and dist.is_dir()):
        return
    dist_root = dist.resolve()

    assets = dist_root / "assets"
    if assets.is_dir():
        app.mount("/assets", _HashedAssets(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    async def spa_root() -> FileResponse:
        """首页。**必须单独声明**，不能指望 `/{full_path:path}` 兜住它。

        `/` 确实会被下面那条兜底路由以空串命中，但 `_within()` 一律拒绝空路径
        （那是它挡越界的一部分），于是首页拿到 404 `{"detail":"页面不存在"}`。
        实测：`GET /` → 404，而 `GET /detect`、`GET /device/1` 都正常。

        开发时用 Vite（`:3000`）完全碰不到这里 —— **只有单端口模式会中招**，
        而树莓派上跑的正是单端口模式（盒子上没有 dev server），
        表现就是"打开 http://<Pi 的 IP>:8000 只看到 404"。
        """
        return FileResponse(dist_root / "index.html", headers=_spa_headers())

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str, request: Request):  # noqa: ARG001
        if full_path.startswith(_API_PREFIXES):
            raise HTTPException(status_code=404, detail=f"接口不存在：/{full_path}")
        # 空路径（即 `/`）在这里也回首页：与上面显式的 `/` 路由互为兜底，
        # 免得将来路由匹配规则一变，首页又悄悄变成 404。
        candidate = _within(dist_root, full_path) if full_path else dist_root / "index.html"
        # 越界的路径不是"未知前端路由"，而是攻击尝试：直接 404，
        # 不要回退到 index.html 让它看起来像一次正常访问
        if candidate is None:
            raise HTTPException(status_code=404, detail="页面不存在")
        if candidate.is_file():
            return FileResponse(candidate, headers=_spa_headers())
        # 正常的 SPA 深链接（/device、/history… 磁盘上没有对应文件）交给前端路由
        return FileResponse(dist_root / "index.html", headers=_spa_headers())


def main() -> None:
    """`python -m app.main` 直接启动开发服务器。"""
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
    )


if __name__ == "__main__":
    main()
