import { describe, expect, it } from 'vitest'

import {
  NOT_MEASURED,
  TEMP_WARN_C,
  diskFreeText,
  diskHint,
  hostLabel,
  isThrottledProblem,
  memFreeText,
  memHint,
  restartsText,
  tempText,
  tempTone,
  tempUnit,
  throttleDotTone,
  throttleText,
  uptimeText
} from './hostMetrics'
import type { HostStatusOut } from '@/types'

/**
 * 「设备状态」页的展示口径。
 *
 * 这里钉的是**一条约定**（来自 deploy/field-soak.sh）：空值 = "没测到"，不是 0。
 * 开发机上没有 vcgencmd，温度/降频/重启次数天然为 null；若显示成 0，
 * 现场会把一个测不到指标的盒子误判为健康 —— 这正是本文件存在的理由。
 *
 * 之所以测这个纯函数模块而不是挂载 DeviceStatusView.vue：
 * vitest.config.ts 刻意不注册 Element Plus（见其文件注释），该视图用了
 * ElButton/StatCard 且依赖 router，不在单测范围内。而 null→文案 的判断
 * 恰恰是唯一值得回归的部分，抽出来后可以精确断言。
 */

/** 开发机（Windows）：Pi 专属指标全部没测到，通用指标正常 */
const 开发机: HostStatusOut = {
  platform: 'Windows',
  machine: 'AMD64',
  model: null,
  is_raspberry_pi: false,
  cpu_temp_c: null,
  throttled: null,
  throttled_ok: null,
  throttled_flags: [],
  disk_free_mb: 20480,
  disk_total_mb: 102400,
  disk_used_percent: 80,
  mem_free_mb: 8000,
  mem_total_mb: 16000,
  uptime_s: 65,
  restarts: null,
  sampled_at: '2026-10-12T03:00:00Z'
}

/** 树莓派且一切正常 */
const 正常树莓派: HostStatusOut = {
  ...开发机,
  platform: 'Linux',
  machine: 'aarch64',
  model: 'Raspberry Pi 5 Model B Rev 1.0',
  is_raspberry_pi: true,
  cpu_temp_c: 51.2,
  throttled: '0x0',
  throttled_ok: true,
  disk_free_mb: 4096,
  disk_total_mb: 32768,
  disk_used_percent: 87.5,
  uptime_s: 3661,
  restarts: 3
}

/** 树莓派欠压 + 曾经降频：field-soak.sh 里的头号现场故障 */
const 欠压树莓派: HostStatusOut = {
  ...正常树莓派,
  cpu_temp_c: 78.4,
  throttled: '0x50005',
  throttled_ok: false,
  throttled_flags: ['欠压', '正在降频', '曾欠压']
}

describe('"没测到"绝不能显示成 0', () => {
  it('Pi 专属字段为 null 时一律给「未测到」', () => {
    expect(tempText(开发机)).toBe(NOT_MEASURED)
    expect(throttleText(开发机)).toBe(NOT_MEASURED)
    expect(restartsText(开发机)).toBe(NOT_MEASURED)
  })

  it('这些文案都不是 "0" / "0 °C" / "0 次"', () => {
    const 文案 = [tempText(开发机), throttleText(开发机), restartsText(开发机)].map(String)
    for (const text of 文案) {
      expect(text).toBe('未测到')
      // 一旦有人"顺手"改成 0 或 `?? 0`，这条会立刻红
      expect(text).not.toMatch(/\d/)
    }
    // 温度没测到时不能带单位，否则会出现"未测到 °C"这种自相矛盾的组合
    expect(tempUnit(开发机)).toBe('')
  })

  it('磁盘/内存/运行时长任何平台都能测到，照常显示数值', () => {
    expect(diskFreeText(开发机)).toBe('20.0 GB')
    expect(memFreeText(开发机)).toBe('7.8 GB')
    expect(uptimeText(开发机)).toBe('01:05')
    expect(diskHint(开发机)).toBe('已用 80%')
    expect(memHint(开发机)).toBe('共 15.6 GB')
  })

  it('连通用指标都没测到时也不崩、不显示 0', () => {
    const 全空: HostStatusOut = {
      ...开发机,
      disk_free_mb: null,
      disk_total_mb: null,
      disk_used_percent: null,
      mem_free_mb: null,
      mem_total_mb: null,
      uptime_s: null
    }
    expect(diskFreeText(全空)).toBe(NOT_MEASURED)
    expect(memFreeText(全空)).toBe(NOT_MEASURED)
    expect(uptimeText(全空)).toBe(NOT_MEASURED)
    expect(diskHint(全空)).toBe('已用比例未测到')
    expect(memHint(全空)).toBe('总量未测到')
  })

  it('status 还没加载（null/undefined）时不抛异常', () => {
    for (const 空 of [null, undefined]) {
      expect(tempText(空)).toBe(NOT_MEASURED)
      expect(tempUnit(空)).toBe('')
      expect(throttleText(空)).toBe(NOT_MEASURED)
      expect(restartsText(空)).toBe(NOT_MEASURED)
      expect(uptimeText(空)).toBe(NOT_MEASURED)
      expect(isThrottledProblem(空)).toBe(false)
      expect(throttleDotTone(空)).toBe('off')
      expect(hostLabel(空)).toBe('未知设备')
    }
  })
})

describe('降频 / 欠压（现场第一大坑）', () => {
  it('0x0 → 正常且不触发告警块', () => {
    expect(throttleText(正常树莓派)).toBe('正常（0x0）')
    expect(isThrottledProblem(正常树莓派)).toBe(false)
    expect(throttleDotTone(正常树莓派)).toBe('ok')
  })

  it('非 0 掩码 → 异常，且只有明确的 false 才算异常', () => {
    expect(throttleText(欠压树莓派)).toBe('异常（0x50005）')
    expect(isThrottledProblem(欠压树莓派)).toBe(true)
    expect(throttleDotTone(欠压树莓派)).toBe('err')
    // 没测到既不算"异常"也不算"正常"：指示灯必须是中性灰，不能误报绿点
    expect(isThrottledProblem(开发机)).toBe(false)
    expect(throttleDotTone(开发机)).toBe('off')
  })
})

describe('温度色调', () => {
  it('低于阈值不告警，达到 70℃ 起用 warn', () => {
    expect(tempTone(正常树莓派)).toBe('default')
    expect(tempTone(欠压树莓派)).toBe('warn')
    expect(tempTone({ ...正常树莓派, cpu_temp_c: TEMP_WARN_C })).toBe('warn')
    expect(tempTone({ ...正常树莓派, cpu_temp_c: TEMP_WARN_C - 0.1 })).toBe('default')
    // 没测到不能算"温度高"
    expect(tempTone(开发机)).toBe('default')
  })

  it('有温度时带单位、显示原值（不四舍五入掉小数）', () => {
    expect(tempText(正常树莓派)).toBe(51.2)
    expect(tempUnit(正常树莓派)).toBe('°C')
  })
})

describe('设备名称', () => {
  it('优先型号，没有型号时退回 平台 · 架构', () => {
    expect(hostLabel(正常树莓派)).toBe('Raspberry Pi 5 Model B Rev 1.0')
    expect(hostLabel(开发机)).toBe('Windows · AMD64')
  })
})

describe('重启次数', () => {
  it('0 是合法值（真的没重启过），与"没测到"必须区分开', () => {
    expect(restartsText({ ...正常树莓派, restarts: 0 })).toBe('0')
    expect(restartsText(开发机)).toBe('未测到')
  })
})
