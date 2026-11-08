import { afterEach, describe, expect, it, vi } from 'vitest'

// client 是整个网络层，这里只关心"请求长什么样"，所以把它换成一个记录调用的假函数。
// 用 `vi.hoisted` 是因为 `vi.mock` 的工厂会被提升到 import 之上 ——
// 直接引用普通 `const` 会撞上暂时性死区。
const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('./client', () => ({ get }))

import { localTzOffsetMinutes, statsSummary } from './stats'

describe('localTzOffsetMinutes', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('UTC+8 必须返回 480，而不是 -480', () => {
    // 浏览器给的是"本地比 UTC 慢多少分钟"，UTC+8 拿到的是 -480，
    // 而后端要的是"UTC + offset = 本地"。**漏掉那个负号偏差会翻倍**（16 小时），
    // 而且图表看起来依然正常 —— 所以这个符号值得一条专门的断言。
    vi.spyOn(Date.prototype, 'getTimezoneOffset').mockReturnValue(-480)
    expect(localTzOffsetMinutes()).toBe(480)
  })

  it('UTC 以西为负', () => {
    vi.spyOn(Date.prototype, 'getTimezoneOffset').mockReturnValue(300)
    expect(localTzOffsetMinutes()).toBe(-300)
  })
})

describe('statsSummary', () => {
  afterEach(() => {
    get.mockReset()
    vi.restoreAllMocks()
  })

  function paramsOfLastCall(): Record<string, unknown> {
    const [url, params] = get.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(url).toBe('/stats/summary')
    return params
  }

  it('总是带上时区偏移 —— 分桶是后端做的，这里不传就再也补不回来', () => {
    vi.spyOn(Date.prototype, 'getTimezoneOffset').mockReturnValue(-480)
    statsSummary('7d')
    const params = paramsOfLastCall()
    expect(params.range).toBe('7d')
    expect(params.tz_offset_min).toBe(480)
  })

  it('地块与设备只在给了的时候才出现在 query 里', () => {
    statsSummary('30d', { plotId: 3, deviceId: 9 })
    const params = paramsOfLastCall()
    expect(params.plot_id).toBe(3)
    expect(params.device_id).toBe(9)

    statsSummary('30d')
    const without = paramsOfLastCall()
    // 不传就不该出现在 query 里：`plot_id=undefined` 会让后端收到字面量 "undefined"，
    // 而它是个 int 参数 —— 结果是 422，而不是"忽略这个条件"。
    expect('plot_id' in without).toBe(false)
    expect('device_id' in without).toBe(false)
  })
})
