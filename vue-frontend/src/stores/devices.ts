import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  bindDevice as bindDeviceApi,
  listDevices,
  listPendingDevices,
  unbindDevice as unbindDeviceApi,
  updateDevice as updateDeviceApi
} from '@/api/devices'
import { notifyApiError, notifySuccess } from '@/utils/notify'
import type { DeviceOut, DevicePendingOut } from '@/types'

/**
 * 「我的设备」——挂在当前账号下的树莓派盒子。
 *
 * 两条约定：
 *   1. **不做轮询**。列表页自己按 `POLL_MS` 定时调 `loadDevices(true)`，
 *      与「设备状态」页同一套写法（那边也是组件持定时器，而不是 store）。
 *      store 只负责状态与动作，不要既管数据又管生命周期。
 *   2. **`online` 直接取后端给的值，绝不在这里按 `last_seen_at` 重算一遍**。
 *      后端的判定是「60 秒内有心跳**且**内存里真有它的帧」，这里只看得到
 *      `last_seen_at` —— 自己算就会在"盒子还活着但画面已经断了"时把状态点
 *      显示成绿色，而这恰恰是最需要看准的那一刻。
 */
export const useDevicesStore = defineStore('devices', () => {
  const items = ref<DeviceOut[]>([])
  /** 正在等待配对的盒子（列表页的"敲门"提示）。没有配对码，只有名字与倒计时 */
  const pending = ref<DevicePendingOut[]>([])
  const loading = ref(false)
  const busy = ref(false)
  const error = ref<string | null>(null)
  const loaded = ref(false)

  /** 已绑定的盒子数量。侧栏/空状态用得上，不必各自 filter 一遍。 */
  const count = computed(() => items.value.length)

  const onlineCount = computed(() => items.value.filter((device) => device.online).length)

  function byId(id: number): DeviceOut | null {
    return items.value.find((device) => device.id === id) ?? null
  }

  /** 用列表里的数据换掉某一台（改名、解绑返回的新状态都走它），列表里没有就插到最前。 */
  function upsert(device: DeviceOut): void {
    const index = items.value.findIndex((item) => item.id === device.id)
    if (index >= 0) items.value.splice(index, 1, device)
    else items.value.unshift(device)
  }

  function remove(id: number): void {
    items.value = items.value.filter((device) => device.id !== id)
  }

  async function loadDevices(force = false): Promise<void> {
    if (loading.value) return
    if (loaded.value && !force) return
    loading.value = true
    error.value = null
    try {
      // 两路并行取。待配对列表**单独兜错**：它挂了不该连已绑定列表一起报错
      // （最常见的失败是刚绑完、那台设备已不在 pending 里 —— 后端会正常给空表，
      // 真正会抛的只有网络层错误，那就静默降级，下一轮轮询再试）。
      const [bound, knocking] = await Promise.allSettled([listDevices(), listPendingDevices()])
      if (bound.status === 'fulfilled') {
        items.value = bound.value
        loaded.value = true
      } else {
        error.value = notifyApiError(bound.reason)
      }
      if (knocking.status === 'fulfilled') pending.value = knocking.value
    } finally {
      loading.value = false
    }
  }

  /**
   * 用 6 位配对码绑定一台盒子。成功返回那台设备，失败返回 `null`（弹窗据此决定不关）。
   *
   * 三种失败的中文提示由**后端**给出（400 码无效/过期、409 已被绑定、429 尝试过多），
   * 这里原样透传，不再自己按状态码写一套文案 —— 两处文案迟早会不一致。
   */
  async function bind(code: string): Promise<DeviceOut | null> {
    busy.value = true
    error.value = null
    try {
      const device = await bindDeviceApi(code.trim())
      upsert(device)
      // 绑定的那台从"敲门"名单里摘掉；别的盒子（若有）保持原样
      pending.value = pending.value.filter((box) => box.id !== device.id)
      notifySuccess(`已绑定设备「${device.name}」`)
      return device
    } catch (err) {
      error.value = notifyApiError(err)
      return null
    } finally {
      busy.value = false
    }
  }

  async function rename(id: number, name: string): Promise<boolean> {
    const trimmed = name.trim()
    if (!trimmed) {
      error.value = '设备名称不能为空'
      return false
    }
    busy.value = true
    error.value = null
    try {
      upsert(await updateDeviceApi(id, { name: trimmed }))
      notifySuccess('设备名称已更新')
      return true
    } catch (err) {
      error.value = notifyApiError(err)
      return false
    } finally {
      busy.value = false
    }
  }

  /**
   * 解绑。**不删历史结果** —— 后端只是把设备标记为 unbound 并清掉令牌，
   * 已上报的任务行还在（`Task.device_id` 是 `SET NULL`，任务会留下但不再指向设备）。
   * 所以这里也**不**把任务从任何缓存里抹掉。
   */
  async function unbind(id: number): Promise<boolean> {
    busy.value = true
    error.value = null
    try {
      await unbindDeviceApi(id)
      remove(id)
      notifySuccess('已解绑，盒子会重新显示配对码')
      return true
    } catch (err) {
      error.value = notifyApiError(err)
      return false
    } finally {
      busy.value = false
    }
  }

  function clearError(): void {
    error.value = null
  }

  function reset(): void {
    items.value = []
    loaded.value = false
    error.value = null
  }

  return {
    items,
    pending,
    loading,
    busy,
    error,
    loaded,
    count,
    onlineCount,
    byId,
    upsert,
    remove,
    loadDevices,
    bind,
    rename,
    unbind,
    clearError,
    reset
  }
})
