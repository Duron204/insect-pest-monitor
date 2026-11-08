/**
 * 设备 store 的契约测试。
 *
 * 这个仓库此前没有打过桩（配置里写明"只跑纯逻辑与自包含组件"），这里破例，
 * 因为 store 有两个**只靠读代码看不出来、错了却是用户可见故障**的性质：
 *
 *   1. `bind()` 失败时返回 `null` —— `PairingDialog` 正是靠这个决定关不关弹窗。
 *      如果它退化成"总是返回对象"，输错码也会把弹窗关掉，用户看不到原因。
 *   2. `bind()` / `unbind()` 失败时**列表必须原样不动**。
 *      `unbind` 提前把行删掉的话，用户会以为解绑成功了，刷新一下它又在。
 *
 * 只打桩 API 与提示，store 自身跑真代码。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// `listPendingDevices` 必须一起打桩：`loadDevices` 里它与 `listDevices` 并行取，
// 缺了这个导出，所有走 loadDevices 的用例都会以"mock 上没有这个导出"失败。
// （踩过：加了待配对提示之后忘了补这里，7 条用例一起红。）
vi.mock('@/api/devices', () => ({
  listDevices: vi.fn(),
  listPendingDevices: vi.fn(async () => []),
  bindDevice: vi.fn(),
  updateDevice: vi.fn(),
  unbindDevice: vi.fn()
}))

vi.mock('@/utils/notify', () => ({
  notifySuccess: vi.fn(),
  notifyApiError: vi.fn(() => '配对码无效或已过期'),
  notifyError: vi.fn(),
  notifyInfo: vi.fn(),
  notifyWarning: vi.fn()
}))

import * as devicesApi from '@/api/devices'
import { notifyApiError, notifySuccess } from '@/utils/notify'
import { useDevicesStore } from '@/stores/devices'
import type { DeviceOut } from '@/types'

function device(id: number, overrides: Partial<DeviceOut> = {}): DeviceOut {
  return {
    id,
    hardware_id: `pi-000${id}`,
    name: `盒子 ${id}`,
    model: 'Raspberry Pi 5 Model B',
    status: 'active',
    plot_id: null,
    agent_version: '0.1.0',
    backend: 'onnx',
    last_seen_at: '2026-10-16T06:00:00',
    last_frame_at: '2026-10-16T06:00:00',
    last_task_at: '2026-10-16T05:30:00',
    created_at: '2026-10-01T00:00:00',
    bound_at: '2026-10-01T00:05:00',
    online: true,
    stream_fps: 2,
    // 盒子自报的诊断字段：默认全是 null（盒子没上报过），
    // 要断言面板行为时在自己的 overrides 里单独给值
    agent_streaming: null,
    agent_stream_enabled: null,
    agent_live_detection: null,
    agent_frames_sent: null,
    agent_uploads: null,
    agent_pending_uploads: null,
    agent_last_error: null,
    agent_last_error_at: null,
    agent_status_at: null,
    agent_camera_backend: null,
    ...overrides
  }
}

const listMock = vi.mocked(devicesApi.listDevices)
const bindMock = vi.mocked(devicesApi.bindDevice)
const updateMock = vi.mocked(devicesApi.updateDevice)
const unbindMock = vi.mocked(devicesApi.unbindDevice)
const notifyErrorMock = vi.mocked(notifyApiError)

describe('设备 store', () => {
  let store: ReturnType<typeof useDevicesStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    store = useDevicesStore()
    vi.clearAllMocks()
    notifyErrorMock.mockReturnValue('配对码无效或已过期')
  })

  it('loadDevices 拉回列表，非 force 时不重复请求', async () => {
    listMock.mockResolvedValue([device(1), device(2, { online: false })])

    await store.loadDevices()
    expect(store.count).toBe(2)
    expect(store.onlineCount).toBe(1)

    await store.loadDevices()
    expect(listMock).toHaveBeenCalledTimes(1)

    await store.loadDevices(true)
    expect(listMock).toHaveBeenCalledTimes(2)
  })

  it('列表接口失败时记下错误，但不把已有列表清空', async () => {
    listMock.mockResolvedValueOnce([device(1)])
    await store.loadDevices()

    listMock.mockRejectedValueOnce(new Error('网络断了'))
    await store.loadDevices(true)

    expect(store.error).toBe('配对码无效或已过期') // 来自打桩的 notifyApiError
    expect(store.count).toBe(1)
  })

  /* ------------------------------- 绑定 ------------------------------- */

  it('绑定成功：返回那台设备并进入列表', async () => {
    bindMock.mockResolvedValue(device(9, { name: '东头盒子' }))

    const bound = await store.bind('123456')

    expect(bound?.id).toBe(9)
    expect(store.byId(9)?.name).toBe('东头盒子')
    expect(notifySuccess).toHaveBeenCalled()
    expect(store.busy).toBe(false)
  })

  it('⭐ 绑定失败必须返回 null 且不动列表 —— 弹窗靠这个决定不关', async () => {
    listMock.mockResolvedValue([device(1)])
    await store.loadDevices()

    bindMock.mockRejectedValue(new Error('配对码无效或已过期'))
    const bound = await store.bind('000000')

    expect(bound).toBeNull()
    expect(store.count).toBe(1) // 原有那台还在，也没有多出别的
    // 后端给的原因要能被弹窗读到（store.error 就是弹窗显示的那行字）
    expect(store.error).toBe('配对码无效或已过期')
    expect(store.busy).toBe(false)
  })

  it('重复绑定同一台不会在列表里出现两行（upsert 而不是 push）', async () => {
    bindMock.mockResolvedValue(device(9))
    await store.bind('123456')
    bindMock.mockResolvedValue(device(9, { name: '改过名的盒子' }))
    await store.bind('654321')

    expect(store.count).toBe(1)
    expect(store.byId(9)?.name).toBe('改过名的盒子')
  })

  it('配对码前后的空格不会带到后端', async () => {
    bindMock.mockResolvedValue(device(9))
    await store.bind('  123456  ')
    expect(bindMock).toHaveBeenCalledWith('123456')
  })

  /* ------------------------------- 改名 ------------------------------- */

  it('改名会 trim；空名字直接拒绝，不发请求（后端 min_length=1 会回 422）', async () => {
    const ok = await store.rename(1, '   ')

    expect(ok).toBe(false)
    expect(store.error).toBe('设备名称不能为空')
    expect(updateMock).not.toHaveBeenCalled()
  })

  it('改名成功后用返回的对象换掉列表里那一行', async () => {
    listMock.mockResolvedValue([device(1)])
    await store.loadDevices()

    updateMock.mockResolvedValue(device(1, { name: '西头盒子' }))
    expect(await store.rename(1, '  西头盒子 ')).toBe(true)

    expect(updateMock).toHaveBeenCalledWith(1, { name: '西头盒子' })
    expect(store.byId(1)?.name).toBe('西头盒子')
    expect(store.count).toBe(1)
  })

  /* ------------------------------- 解绑 ------------------------------- */

  it('⭐ 解绑成功才从列表移除；失败必须原样留着', async () => {
    listMock.mockResolvedValue([device(1), device(2)])
    await store.loadDevices()

    unbindMock.mockRejectedValueOnce(new Error('设备不存在'))
    expect(await store.unbind(1)).toBe(false)
    expect(store.count).toBe(2) // 还在 —— 否则用户以为解绑成功了

    unbindMock.mockResolvedValueOnce(device(1, { status: 'unbound' }))
    expect(await store.unbind(1)).toBe(true)
    expect(store.count).toBe(1)
    expect(store.byId(1)).toBeNull()
    expect(store.byId(2)).not.toBeNull()
  })

  it('reset 清空并让下次 loadDevices 重新请求', async () => {
    listMock.mockResolvedValue([device(1)])
    await store.loadDevices()
    store.reset()
    expect(store.count).toBe(0)

    listMock.mockResolvedValue([device(1)])
    await store.loadDevices()
    expect(listMock).toHaveBeenCalledTimes(2)
  })

  it('online 直接取后端给的值，不在前端按 last_seen_at 重算', async () => {
    // 心跳是新的，但后端已经判定离线（比如内存里那一帧被清了）—— 以前端为准就会显示成绿色
    listMock.mockResolvedValue([
      device(1, { online: false, last_seen_at: new Date().toISOString() })
    ])
    await store.loadDevices()

    expect(store.items[0]?.online).toBe(false)
    expect(store.onlineCount).toBe(0)
  })
})
