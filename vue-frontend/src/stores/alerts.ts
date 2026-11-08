import { ref } from 'vue'
import { defineStore } from 'pinia'
import { listAlerts } from '@/api/alerts'

/**
 * 未确认告警数（供侧栏导航显示红点）。
 *
 * 单独放一个 store 是因为它有两个写入方：定时刷新（App 挂载时启动）
 * 与用户手动确认（告警中心页面）。放进组件里会导致两处状态不同步。
 *
 * 刻意"失败即忽略"：取不到数量只影响一个红点，不该弹错误、更不该
 * 影响页面本身 —— 用户仍然可以正常进告警中心看列表。
 */
export const useAlertsStore = defineStore('alerts', () => {
  const unacked = ref(0)
  const loading = ref(false)

  // 与 history store 同一个理由：在途的刷新必须能在换账号时作废，
  // 否则 A 的请求晚回来，会把 A 的未读数写到 B 的红点上。
  let requestSeq = 0

  async function refresh(): Promise<void> {
    requestSeq += 1
    const seq = requestSeq
    loading.value = true
    try {
      const rows = await listAlerts({ acked: false, limit: 500 })
      if (seq === requestSeq) unacked.value = rows.length
    } catch {
      // 有意留空：红点拿不到就不显示，不影响任何页面功能
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  function setUnacked(value: number): void {
    unacked.value = Math.max(0, value)
  }

  /** 换账号时清空红点（见 stores/user.ts）。 */
  function reset(): void {
    requestSeq += 1
    unacked.value = 0
    loading.value = false
  }

  return { unacked, loading, refresh, setUnacked, reset }
})
