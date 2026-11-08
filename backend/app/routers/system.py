# -*- coding: utf-8 -*-
"""主机状态接口（「设备状态」页的数据源）。

两个接口：`/system/host` 是本机健康指标，`/system/agent` 是**边缘代理**
（田里的盒子外拨给中心）的状态 —— 后者是 pairing 配对码在网页上的唯一出处。

**为什么不塞进 `/api/health`**：那个接口是**刻意不鉴权**的（登录页要用它判断
后端起没起来），而这里要吐磁盘剩余、内存、温度这类主机信息 —— 混进同一个
router，迟早有人为了省事把它一起设成公开。独立 router 自带鉴权边界。

现场部署时后端就跑在树莓派上，所以"主机状态"就是**树莓派自己的状态**；
开发机上则是开发机自己（温度/降频会是"未测到"，这是预期行为）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import get_current_user
from ..models import User
from ..schemas import AgentLiveDetectionIn, AgentStatusOut, HostStatusOut
from ..services.hostinfo import collect_host_metrics

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/host", response_model=HostStatusOut)
def host_status(
    request: Request,
    _: User = Depends(get_current_user),
) -> HostStatusOut:
    """主机健康：温度 / 降频欠压 / 磁盘 / 内存 / 运行时长 / 重启次数。

    写成**同步** `def`（而不是 `async def`）：采集要起 vcgencmd 子进程、读
    /proc、查 systemd，全是阻塞调用。同步路由会被 FastAPI 丢进线程池，
    不会卡住事件循环 —— 项目里已经有几处"在 async 里干阻塞活"的既有问题，
    这里不再新增同款。

    拿不到的指标一律返回 None（"没测到"），接口始终 200。
    """
    settings = request.app.state.settings
    # 要量的是**会写满的那个卷**：现场填满 SD 卡的是 outputs/uploads，不是 /。
    # outputs 目录可能还没建（首次启动），那就退回项目根 —— 两者通常在同一个卷上。
    disk_path = settings.abs_outputs_dir
    if not disk_path.exists():
        disk_path = settings.project_root

    metrics = collect_host_metrics(disk_path=disk_path)
    return HostStatusOut.model_validate(metrics)


@router.get("/agent", response_model=AgentStatusOut)
def agent_status(
    request: Request,
    _: User = Depends(get_current_user),
) -> AgentStatusOut:
    """这台机器上的边缘代理状态（配对码、推流、上报计数、最近错误）。

    **为什么需要一个接口而不是只看日志**：配对码是在 Pi 的终端上打印的，
    但现场往往没有终端 —— 手机上打开这台 Pi 自己的网页就能看到码，
    这一条把"必须 ssh 上去 journalctl"变成了"打开网页"。

    需要登录：它和 `/system/host` 属于同一类"本机运维信息"。
    这不构成新的泄漏面 —— 那个配对码本来就会被打进本机的日志。
    """
    agent = getattr(request.app.state, "agent", None)
    if agent is None:  # pragma: no cover - 只有装配出错才会走到
        return AgentStatusOut()
    return AgentStatusOut.model_validate(agent.status())


@router.post("/agent/live-detection", response_model=AgentStatusOut)
def set_agent_live_detection(
    payload: AgentLiveDetectionIn,
    request: Request,
    _: User = Depends(get_current_user),
) -> AgentStatusOut:
    """开启/关闭推流画面上的**本机实时识别**，并落盘到 `agent.json`。

    这是给**树莓派自己的设置页**用的：中心不能反向连盒子，所以中心页面
    的开关无从谈起；这条接口只影响运行它的这台机器。

    为什么不是 404/403：`agent.enabled=false`（例如开发机、中心机）与
    `stream_enabled=false` 都不是权限问题，而是"这台机器现在没有可识别的
    推流"。409 能让人看懂要改哪一项配置。

    写成同步 `def`：`set_live_detection` 里有一次落盘写，FastAPI 会把它
    丢进线程池，不占事件循环。
    """
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="边缘代理服务未初始化",
        )

    current = agent.status()
    if not current["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="边缘代理未启用（agent.enabled=false），这台机器没有推流可识别",
        )
    if not current["stream_enabled"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="配置已关闭推流（agent.stream_enabled=false），实时识别没有可处理的画面",
        )

    return AgentStatusOut.model_validate(agent.set_live_detection(payload.enabled))
