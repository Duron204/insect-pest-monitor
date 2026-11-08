import { formatBytes, formatDurationMs } from './index'
import type { HostStatusOut } from '@/types'

/**
 * 「设备状态」页的展示格式化。
 *
 * 单独抽出来是为了能**只测纯逻辑**：vitest.config.ts 刻意不注册 Element Plus
 * （见其文件注释），所以带 `StatCard`/`ElButton` 的视图不在单测范围内。
 * 把"怎么把 null 显示出来"这段逻辑挪到这里，正是最该被测、也最好测的部分。
 *
 * **本文件存在的唯一理由**：后端契约里 `null` 表示"没测到"而不是 0
 * （口径来自 deploy/field-soak.sh）。开发机上没有 vcgencmd，温度/降频/重启次数
 * 全是 null —— 若渲染成 0，现场会把一个测不到指标的盒子误判为健康。
 * 这里提供唯一的出口，保证每一处都走同一套判断。
 */

/** 统一的"没测到"文案 —— 不要在各处手写字符串 */
export const NOT_MEASURED = '未测到'

/** 超过这个温度就要留意散热（Pi 5 在 80℃ 附近开始软降频） */
export const TEMP_WARN_C = 70

type Status = HostStatusOut | null | undefined

function measured(value: number | null | undefined): value is number {
  return value !== null && value !== undefined && Number.isFinite(value)
}

function mbText(value: number | null | undefined): string {
  return measured(value) ? formatBytes(value * 1024 * 1024) : NOT_MEASURED
}

export function tempText(status: Status): number | string {
  const value = status?.cpu_temp_c
  return measured(value) ? value : NOT_MEASURED
}

/** 温度没测到时不要显示单位，否则会出现"未测到 °C"这种自相矛盾的组合 */
export function tempUnit(status: Status): string {
  return measured(status?.cpu_temp_c) ? '°C' : ''
}

export function tempTone(status: Status): 'warn' | 'default' {
  const value = status?.cpu_temp_c
  return measured(value) && value >= TEMP_WARN_C ? 'warn' : 'default'
}

export function diskFreeText(status: Status): string {
  return mbText(status?.disk_free_mb)
}

export function diskHint(status: Status): string {
  const used = status?.disk_used_percent
  return measured(used) ? `已用 ${used}%` : '已用比例未测到'
}

export function memFreeText(status: Status): string {
  return mbText(status?.mem_free_mb)
}

export function memHint(status: Status): string {
  const total = status?.mem_total_mb
  return measured(total) ? `共 ${formatBytes(total * 1024 * 1024)}` : '总量未测到'
}

export function uptimeText(status: Status): string {
  const seconds = status?.uptime_s
  return measured(seconds) ? formatDurationMs(seconds * 1000) : NOT_MEASURED
}

/** 重启次数：非 systemd 部署下后端返回 null，这里绝不能变成"0 次" */
export function restartsText(status: Status): string {
  const value = status?.restarts
  return measured(value) ? String(value) : NOT_MEASURED
}

/** 降频/欠压：把 0x0 与"没测到"分开说清楚 */
export function throttleText(status: Status): string {
  const ok = status?.throttled_ok
  if (ok === null || ok === undefined) return NOT_MEASURED
  return ok ? `正常（${status?.throttled ?? '0x0'}）` : `异常（${status?.throttled ?? '?'}）`
}

/** 只有明确 false（有欠压/降频）才算异常；没测到不能算"异常"也不能算"正常" */
export function isThrottledProblem(status: Status): boolean {
  return status?.throttled_ok === false
}

export function throttleDotTone(status: Status): 'ok' | 'err' | 'off' {
  const ok = status?.throttled_ok
  if (ok === null || ok === undefined) return 'off'
  return ok ? 'ok' : 'err'
}

/** 非树莓派时页面上要明说"当前管的是哪台机器"，避免把开发机误当成现场设备 */
export function hostLabel(status: Status): string {
  if (!status) return '未知设备'
  return status.model ?? `${status.platform} · ${status.machine}`
}
