import { describe, expect, it } from 'vitest'

import {
  TASK_MODE_LABELS,
  TASK_STATUS_LABELS,
  baseName,
  classNames,
  errorText,
  formatBytes,
  formatDate,
  formatDateTime,
  formatDurationMs,
  formatMs,
  formatNumber,
  formatPercent,
  formatRatio,
  formatRelative,
  formatTaskNo,
  sortedCountEntries,
  sumValues,
  taskModeLabel,
  taskStatusLabel,
  taskStatusTone,
  toIsoParam,
  truncate,
  uniqueSorted
} from './index'

/**
 * 这些函数是"后端数据 → 界面文字"的唯一通道，出错会直接显示给用户
 * （例如把 NaN / Invalid Date 漏到页面上）。所以重点钉住边界与空值。
 */

describe('数字格式化', () => {
  it('空值与非法数字一律显示破折号，绝不把 NaN/Infinity 漏到界面', () => {
    for (const value of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(formatNumber(value)).toBe('—')
      expect(formatRatio(value)).toBe('—')
      expect(formatMs(value)).toBe('—')
      expect(formatBytes(value)).toBe('—')
      expect(formatPercent(value)).toBe('—')
      expect(formatDurationMs(value)).toBe('—')
    }
  })

  it('formatNumber 保留固定小数位并加千分位', () => {
    expect(formatNumber(7)).toBe('7')
    expect(formatNumber(1234.5678, 2)).toBe('1,234.57')
    expect(formatNumber(0.5, 1)).toBe('0.5')
  })

  it('formatRatio 用于 mAP 这类小数', () => {
    expect(formatRatio(0.76759, 3)).toBe('0.768')
    expect(formatRatio(0.9)).toBe('0.90')
  })

  it('formatMs 在 1 秒处切换单位', () => {
    expect(formatMs(999)).toBe('999 ms')
    expect(formatMs(1500)).toBe('1.50 s')
  })

  it('formatDurationMs 输出 MM:SS，超过一小时才带小时', () => {
    expect(formatDurationMs(65_000)).toBe('01:05')
    expect(formatDurationMs(3_661_000)).toBe('1:01:01')
    // 负数（时钟回拨等异常）应被夹到 0，而不是显示 -1:-5
    expect(formatDurationMs(-5)).toBe('00:00')
  })

  it('formatBytes 按 1024 进位', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1023)).toBe('1023 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB')
    expect(formatBytes(2 * 1024 ** 3)).toBe('2.0 GB')
  })

  it('formatPercent 直接拼百分号（调用方给的就是百分数）', () => {
    expect(formatPercent(12.34, 1)).toBe('12.3%')
    expect(formatPercent(50)).toBe('50%')
  })
})

describe('时间格式化', () => {
  it('不带时区的 ISO 串按 UTC 解释（后端就是这么存的）', () => {
    // 时区无关的断言：补 Z 与显式带 Z 必须得到同一结果
    expect(formatDateTime('2026-10-10T06:00:00')).toBe(formatDateTime('2026-10-10T06:00:00Z'))
    expect(formatDateTime('2026-10-10T06:00:00.123456')).toBe(
      formatDateTime('2026-10-10T06:00:00.123456Z')
    )
  })

  it('已经带时区的串按原意解释', () => {
    expect(formatDateTime('2026-10-10T06:00:00+08:00')).toBe(
      formatDateTime('2026-10-09T22:00:00Z')
    )
  })

  it('无法解析的时间显示破折号', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('')).toBe('—')
    expect(formatDateTime('不是时间')).toBe('—')
    expect(formatDate('不是时间')).toBe('—')
  })

  it('formatDate 只到日', () => {
    const text = formatDate('2026-10-10T06:00:00Z')
    expect(text).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('formatRelative 覆盖刚刚/分钟/小时/天，超过 30 天退回日期', () => {
    expect(formatRelative(new Date(Date.now() + 60_000).toISOString())).toBe('刚刚')
    expect(formatRelative(new Date(Date.now() - 30_000).toISOString())).toBe('30 秒前')
    expect(formatRelative(new Date(Date.now() - 5 * 60_000).toISOString())).toBe('5 分钟前')
    expect(formatRelative(new Date(Date.now() - 3 * 3600_000).toISOString())).toBe('3 小时前')
    expect(formatRelative(new Date(Date.now() - 2 * 86400_000).toISOString())).toBe('2 天前')
    const old = new Date(Date.now() - 90 * 86400_000).toISOString()
    expect(formatRelative(old)).toBe(formatDate(old))
  })

  it('toIsoParam 供查询参数使用，非法值返回 undefined', () => {
    expect(toIsoParam(null)).toBeUndefined()
    expect(toIsoParam('')).toBeUndefined()
    expect(toIsoParam('不是时间')).toBeUndefined()
    expect(toIsoParam('2026-10-10T06:00:00Z')).toBe('2026-10-10T06:00:00.000Z')
  })
})

describe('任务展示映射', () => {
  it('本机抓拍必须有中文文案（曾经漏掉，界面直接显示英文 local）', () => {
    expect(TASK_MODE_LABELS.local).toBe('本机抓拍')
    // 后端 TaskMode 的全部取值都要有文案，否则界面上会露出英文。
    // `device` = 绑定的树莓派上报的抓拍，与 `local`（跑后端这台机器自己抓的）是两回事。
    expect(TASK_MODE_LABELS.device).toBe('设备上报')
    for (const mode of ['image', 'folder', 'video', 'camera', 'local', 'device']) {
      expect(TASK_MODE_LABELS[mode]).toBeTruthy()
    }
    for (const status of ['pending', 'running', 'done', 'failed']) {
      expect(TASK_STATUS_LABELS[status]).toBeTruthy()
    }
  })

  it('未知取值回退原文案，空值给破折号', () => {
    expect(taskModeLabel('local')).toBe('本机抓拍')
    expect(taskModeLabel('brand-new-mode')).toBe('brand-new-mode')
    expect(taskModeLabel(null)).toBe('—')
    expect(taskStatusLabel('done')).toBe('已完成')
    expect(taskStatusLabel(undefined)).toBe('—')
  })

  it('状态色调映射与后端状态一一对应', () => {
    expect(taskStatusTone('done')).toBe('ok')
    expect(taskStatusTone('running')).toBe('warn')
    expect(taskStatusTone('pending')).toBe('warn')
    expect(taskStatusTone('failed')).toBe('err')
    expect(taskStatusTone('whatever')).toBe('idle')
    expect(taskStatusTone(null)).toBe('idle')
  })

  it('formatTaskNo 统一 6 位', () => {
    expect(formatTaskNo(4)).toBe('#000004')
    expect(formatTaskNo(1234567)).toBe('#1234567')
    expect(formatTaskNo(null)).toBe('—')
  })

  it('baseName 同时处理正反斜杠路径（Windows 后端会回传反斜杠）', () => {
    expect(baseName('C:\\data\\weights\\best.pt')).toBe('best.pt')
    expect(baseName('/opt/insect/outputs/000001/annotated.jpg')).toBe('annotated.jpg')
    expect(baseName('bare.jpg')).toBe('bare.jpg')
    expect(baseName('')).toBe('')
    expect(baseName(null)).toBe('')
  })
})

describe('集合与字符串', () => {
  it('sortedCountEntries 按数量降序', () => {
    expect(sortedCountEntries({ 菜螟: 2, 小菜蛾: 7 })).toEqual([
      ['小菜蛾', 7],
      ['菜螟', 2]
    ])
    expect(sortedCountEntries(null)).toEqual([])
  })

  it('sumValues 求和，空返回 0', () => {
    expect(sumValues({ a: 1, b: 2 })).toBe(3)
    expect(sumValues(null)).toBe(0)
    expect(sumValues({})).toBe(0)
  })

  it('uniqueSorted 去空、去重、按中文排序', () => {
    expect(uniqueSorted(['b', null, 'a', 'a', ' b ', undefined])).toEqual(['a', 'b'])
  })

  it('truncate 超长时截断并加省略号', () => {
    expect(truncate('abc', 4)).toBe('abc')
    expect(truncate('abcdef', 4)).toBe('abc…')
    expect(truncate(null)).toBe('')
  })

  it('classNames 过滤假值', () => {
    expect(classNames('a', false, null, undefined, '', 'b')).toBe('a b')
  })

  it('errorText 覆盖 Error/字符串/带 message 的对象', () => {
    expect(errorText(new Error('炸了'))).toBe('炸了')
    expect(errorText('纯字符串')).toBe('纯字符串')
    expect(errorText({ message: '对象里的消息' })).toBe('对象里的消息')
    expect(errorText(123)).toBe('未知错误')
  })
})
