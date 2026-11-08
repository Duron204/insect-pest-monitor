import { get, post } from './client'
import type { AgentStatusOut, HealthOut, HostStatusOut } from '@/types'

/* ---------------------------- 系统健康 ---------------------------- */

export function health(): Promise<HealthOut> {
  return get<HealthOut>('/health')
}

/**
 * 主机健康（「设备状态」页）。
 *
 * 与 `health()` 的区别：那个报的是**推理后端/模型**状态且不鉴权（登录页要用），
 * 这个报的是**运行后端的这台机器自己**（现场＝树莓派）的温度/降频/磁盘/内存，
 * 因此需要鉴权。
 */
export function hostStatus(): Promise<HostStatusOut> {
  return get<HostStatusOut>('/system/host')
}

/**
 * 本机边缘代理状态（配对、推流、实时识别开关）。
 *
 * 与 `hostStatus()` 的区别：那个是温度/磁盘，这个是"盒子挂没挂上中心、
 * 有没有在推流"。设置页的「实时识别」开关靠它决定显示与初始状态。
 */
export function agentStatus(): Promise<AgentStatusOut> {
  return get<AgentStatusOut>('/system/agent')
}

/**
 * 打开/关闭推流画面的本机实时识别。
 *
 * 只对**运行后端的这台机器**生效：中心不能反向连盒子，所以这个接口
 * 必须在盒子自己的网页上调。返回完整状态，调用方直接用它刷新开关。
 */
export function setLiveDetection(enabled: boolean): Promise<AgentStatusOut> {
  return post<AgentStatusOut>('/system/agent/live-detection', { enabled })
}
