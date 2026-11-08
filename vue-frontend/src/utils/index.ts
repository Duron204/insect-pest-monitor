import type { CountMap } from '@/types'

/**
 * 数组安全访问。
 * tsconfig 开启了 noUncheckedIndexedAccess，下标访问得到 T | undefined，
 * 统一通过这里取值可以避免类型报错并给出确定性的兜底。
 */
export function at<T>(list: readonly T[] | null | undefined, index: number, fallback: T): T {
  if (!list) return fallback
  if (!Number.isInteger(index) || index < 0 || index >= list.length) return fallback
  const value = list[index]
  return value === undefined ? fallback : value
}

export function firstOr<T>(list: readonly T[] | null | undefined, fallback: T): T {
  return at(list, 0, fallback)
}

/** 取 Record 的值（index signature 同样是 T | undefined） */
export function recordGet(map: CountMap | Record<string, number>, key: string): number {
  return map[key] ?? 0
}

export function isEmptyRecord(map: CountMap | Record<string, number> | null | undefined): boolean {
  if (!map) return true
  return Object.keys(map).length === 0
}

/* ------------------------------- 数字 ------------------------------- */

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })
}

export function formatRatio(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toFixed(digits)
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value < 1000) return `${value.toFixed(0)} ms`
  return `${(value / 1000).toFixed(2)} s`
}

export function formatDurationMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const totalSeconds = Math.max(0, Math.round(value / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit] ?? 'B'}`
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)}%`
}

/* ------------------------------- 时间 ------------------------------- */

function parseDate(input: string | null | undefined): Date | null {
  if (!input) return null
  // 后端可能返回不带时区的 ISO 串（UTC），补 Z 以便正确本地化
  const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(input) ? input : `${input}Z`
  const date = new Date(normalized)
  if (Number.isNaN(date.getTime())) {
    const fallback = new Date(input)
    return Number.isNaN(fallback.getTime()) ? null : fallback
  }
  return date
}

export function formatDateTime(input: string | null | undefined): string {
  const date = parseDate(input)
  if (!date) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function formatDate(input: string | null | undefined): string {
  const date = parseDate(input)
  if (!date) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function formatRelative(input: string | null | undefined): string {
  const date = parseDate(input)
  if (!date) return '—'
  const diff = Date.now() - date.getTime()
  if (diff < 0) return '刚刚'
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return formatDate(input)
}

/** 用于后端 since/until 查询参数（ISO 8601，不带时区的本地时间亦可被 FastAPI 解析） */
export function toIsoParam(value: Date | string | null | undefined): string | undefined {
  if (!value) return undefined
  const date = typeof value === 'string' ? new Date(value) : value
  if (Number.isNaN(date.getTime())) return undefined
  return date.toISOString()
}

/* ------------------------------- 任务展示 ------------------------------- */

export const TASK_MODE_LABELS: Record<string, string> = {
  image: '单图检测',
  folder: '批量检测',
  video: '视频检测',
  camera: '摄像头实时',
  local: '本机抓拍',
  // 绑定的树莓派定时抓拍后上报的结果。与「本机抓拍」的区别是**哪台机器拍的**：
  // local 是跑后端这台机器自己拍的，device 是田里那台盒子报上来的。
  device: '设备上报'
}

export const TASK_STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  running: '进行中',
  done: '已完成',
  failed: '失败'
}

export type StatusTone = 'ok' | 'warn' | 'err' | 'info' | 'idle'

export function taskStatusTone(status: string | null | undefined): StatusTone {
  switch (status) {
    case 'done':
      return 'ok'
    case 'running':
    case 'pending':
      return 'warn'
    case 'failed':
      return 'err'
    default:
      return 'idle'
  }
}

export function taskModeLabel(mode: string | null | undefined): string {
  if (!mode) return '—'
  return TASK_MODE_LABELS[mode] ?? mode
}

export function taskStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return TASK_STATUS_LABELS[status] ?? status
}

/** 任务号统一 6 位零填充展示 */
export function formatTaskNo(id: number | null | undefined): string {
  if (id === null || id === undefined) return '—'
  return `#${String(id).padStart(6, '0')}`
}

/** 取文件名（去掉目录） */
export function baseName(path: string | null | undefined): string {
  if (!path) return ''
  const normalized = path.replace(/\\/g, '/')
  const parts = normalized.split('/')
  const last = parts[parts.length - 1]
  return last && last.length > 0 ? last : path
}

/* ------------------------------- 集合 ------------------------------- */

export function sortedCountEntries(map: CountMap | null | undefined): [string, number][] {
  if (!map) return []
  return Object.entries(map).sort((a, b) => b[1] - a[1])
}

export function sumValues(map: CountMap | null | undefined): number {
  if (!map) return 0
  return Object.values(map).reduce((acc, value) => acc + value, 0)
}

export function uniqueSorted(values: (string | number | null | undefined)[]): string[] {
  const set = new Set<string>()
  for (const value of values) {
    if (value === null || value === undefined) continue
    const text = String(value).trim()
    if (text.length > 0) set.add(text)
  }
  return [...set].sort((a, b) => a.localeCompare(b, 'zh-CN'))
}

export function classNames(...values: (string | false | null | undefined)[]): string {
  return values.filter((value): value is string => typeof value === 'string' && value.length > 0).join(' ')
}

/* ------------------------------- 字符串 ------------------------------- */

export function truncate(text: string | null | undefined, max = 40): string {
  if (!text) return ''
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`
}

export function errorText(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return '未知错误'
}
