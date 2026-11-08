import { get } from './client'
import type { StatsRange, StatsSummaryOut } from '@/types'

/* ------------------------------- 统计 ------------------------------- */

/**
 * 汇总的过滤条件。
 *
 * **为什么是具名对象而不是两个并列的可选位置参数**：`plotId` 与 `deviceId`
 * 都是 `number | undefined`，位置参数一旦写反（`statsSummary(r, deviceId, plotId)`），
 * TypeScript 完全看不出来，症状是"图表画出来了但是别人/别的地块的数据"——
 * 一个看起来正常、查起来很费劲的错误。具名字段让写反变得不可能。
 */
export interface StatsQuery {
  plotId?: number
  /** 只看某台绑定设备的结果。后端会先校验归属（不是我的 → 404），再过滤。 */
  deviceId?: number
}

/**
 * 浏览器所在时区相对 UTC 的分钟偏移（UTC+8 → `480`）。
 *
 * **注意符号**：`Date.prototype.getTimezoneOffset()` 返回的是"本地比 UTC 慢多少分钟"，
 * 所以 UTC+8 拿到的是 **-480**。后端要的是 +480（`UTC + offset = 本地`），
 * 两者正好差一个负号 —— 漏掉这个负号，图表的偏差会**翻倍**（16 小时），
 * 而且依然是"看起来很正常"的错。
 */
export function localTzOffsetMinutes(): number {
  return -new Date().getTimezoneOffset()
}

export function statsSummary(range: StatsRange, query: StatsQuery = {}): Promise<StatsSummaryOut> {
  const params: Record<string, unknown> = { range }
  if (typeof query.plotId === 'number') params.plot_id = query.plotId
  if (typeof query.deviceId === 'number') params.device_id = query.deviceId
  // 后端的 by_day / by_hour 是按这个偏移分桶的（库里存的是裸 UTC）。
  // 分完桶就只剩 24 个数字，前端再也没法把它们挪到本地时间上 —— 所以必须在这里传。
  params.tz_offset_min = localTzOffsetMinutes()
  return get<StatsSummaryOut>('/stats/summary', params)
}

export const STATS_RANGES: { value: StatsRange; label: string }[] = [
  { value: '24h', label: '近 24 小时' },
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: 'all', label: '全部' }
]
