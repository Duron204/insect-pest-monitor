import { API_BASE, get, patch, post } from './client'
import type {
  DeviceOut,
  DevicePendingOut,
  DeviceStreamTicketOut,
  DeviceUpdatePayload,
  PageOut,
  TaskOut
} from '@/types'

/* --------------------------- 我的设备（绑定的树莓派） --------------------------- */
/**
 * 这一组接口全部需要登录，且**按归属隔离**：别人的设备在这里一律表现为 404
 * （不是 403 —— 403 等于确认了"这台设备存在"）。
 *
 * 唯一不在此文件的是设备主动外拨的那两个面（`POST /api/devices/tasks` 与
 * `WS /ws/agent`）：它们用设备令牌鉴权，跑在田里的盒子上，网页从不调用。
 */

export function listDevices(): Promise<DeviceOut[]> {
  return get<DeviceOut[]>('/devices')
}

/**
 * 正在等待配对的盒子。只含"门铃响了"的信息，**没有配对码**（安全设计，
 * 见后端 `list_pending_devices` 的 docstring）—— 码仍然只能从盒子那边抄。
 */
export function listPendingDevices(): Promise<DevicePendingOut[]> {
  return get<DevicePendingOut[]>('/devices/pending')
}

export function getDevice(id: number): Promise<DeviceOut> {
  return get<DeviceOut>(`/devices/${id}`)
}

/**
 * 用 6 位配对码绑定。
 *
 * 三种失败各有明确的中文 `detail`，`normalizeError` 会原样取出来给用户看，
 * 所以这里**不做**状态码到文案的二次映射 —— 两份文案迟早会打架：
 *   400 配对码无效或已过期 / 409 该设备已被绑定，请先在设备页面解绑 /
 *   429 尝试次数过多，请 10 分钟后再试
 */
export function bindDevice(code: string): Promise<DeviceOut> {
  return post<DeviceOut>('/devices/bind', { code })
}

/** 改名，或设置/取消默认地块（`plot_id: null` 表示取消；字段缺席表示不动它）。 */
export function updateDevice(id: number, payload: DeviceUpdatePayload): Promise<DeviceOut> {
  return patch<DeviceOut>(`/devices/${id}`, payload)
}

export function unbindDevice(id: number): Promise<DeviceOut> {
  return post<DeviceOut>(`/devices/${id}/unbind`)
}

export interface DeviceTaskQuery {
  page?: number
  page_size?: number
}

export function listDeviceTasks(
  id: number,
  query: DeviceTaskQuery = {}
): Promise<PageOut<TaskOut>> {
  const params: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params[key] = value
  }
  return get<PageOut<TaskOut>>(`/devices/${id}/tasks`, params)
}

/**
 * 领一张看实时画面的短期票据。
 *
 * 为什么要票据而不是直接把用户 JWT 放进 URL：`<img src>` 设不了请求头，
 * 而 JWT 进 query 会泄漏进访问日志、浏览器历史和 `Referer`。
 * 票据 120 秒过期、只对一台设备有效，损害被限制在"看这一路摄像头两分钟"。
 */
export function createStreamTicket(id: number): Promise<DeviceStreamTicketOut> {
  return post<DeviceStreamTicketOut>(`/devices/${id}/stream-ticket`)
}

/**
 * 拼出可以直接丢给 `<img src>` 的实时画面地址。**纯函数**，好测。
 *
 * `cacheBust` 是给重连用的：`<img>` 在出错后会自动重试**同一个 URL**，
 * 浏览器也可能把上一次失败的响应缓存住，所以换票时必须换一个地址。
 * 传 `Date.now()` 即可。
 */
export function streamUrl(id: number, ticket: string, cacheBust?: number): string {
  const params = new URLSearchParams({ ticket })
  if (cacheBust !== undefined) params.set('t', String(cacheBust))
  return `${API_BASE}/devices/${id}/stream?${params.toString()}`
}
