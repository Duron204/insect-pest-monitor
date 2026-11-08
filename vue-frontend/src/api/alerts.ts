import { get, post } from './client'
import type { AlertOut, AlertRulesOut } from '@/types'

/**
 * 告警中心接口。
 *
 * 规则本身在 `backend/app/data/alert_rules.yaml` —— 前端只**查看**、不在页面上改：
 * 阈值属于生产参数，放在文件里可审计、可版本对比，比塞进界面更稳妥
 * （这是后端刻意的设计决定，页面上也如实这么写）。
 */

export interface AlertQuery {
  acked?: boolean
  level?: string
  plot_id?: number
  limit?: number
}

export function listAlerts(query: AlertQuery = {}): Promise<AlertOut[]> {
  const params: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params[key] = value
  }
  return get<AlertOut[]>('/alerts', params)
}

export function listAlertRules(): Promise<AlertRulesOut> {
  return get<AlertRulesOut>('/alerts/rules')
}

export function ackAlert(id: number): Promise<AlertOut> {
  return post<AlertOut>(`/alerts/${id}/ack`)
}

export function ackAllAlerts(): Promise<{ acked: number }> {
  return post<{ acked: number }>('/alerts/ack-all')
}
