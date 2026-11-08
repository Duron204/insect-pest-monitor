import { ref } from 'vue'
import { defineStore } from 'pinia'
import { deleteTask, getTaskCounts, listTaskDetections, listTasks } from '@/api/tasks'
import { notifyApiError, notifySuccess } from '@/utils/notify'
import { toIsoParam } from '@/utils'
import type { Detection, PageOut, TaskCountsOut, TaskOut } from '@/types'

export interface HistoryFilters {
  mode: string
  status: string
  dateRange: [string, string] | null
}

export const useHistoryStore = defineStore('history', () => {
  const items = ref<TaskOut[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const filters = ref<HistoryFilters>({ mode: '', status: '', dateRange: null })

  let requestSeq = 0

  async function fetch(): Promise<void> {
    requestSeq += 1
    const seq = requestSeq
    loading.value = true
    error.value = null
    try {
      const range = filters.value.dateRange
      const response: PageOut<TaskOut> = await listTasks({
        page: page.value,
        page_size: pageSize.value,
        mode: filters.value.mode || undefined,
        status: filters.value.status || undefined,
        since: range ? toIsoParam(range[0]) : undefined,
        until: range ? toIsoParam(range[1]) : undefined
      })
      if (seq !== requestSeq) return
      items.value = response.items ?? []
      total.value = response.total ?? 0
      page.value = response.page ?? page.value
      pageSize.value = response.page_size ?? pageSize.value
    } catch (err) {
      if (seq !== requestSeq) return
      error.value = notifyApiError(err)
      items.value = []
      total.value = 0
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  async function setPage(next: number): Promise<void> {
    page.value = Math.max(1, next)
    await fetch()
  }

  async function setPageSize(size: number): Promise<void> {
    pageSize.value = size
    page.value = 1
    await fetch()
  }

  async function applyFilters(next: Partial<HistoryFilters>): Promise<void> {
    filters.value = { ...filters.value, ...next }
    page.value = 1
    await fetch()
  }

  async function resetFilters(): Promise<void> {
    filters.value = { mode: '', status: '', dateRange: null }
    page.value = 1
    await fetch()
  }

  async function remove(id: number): Promise<boolean> {
    try {
      await deleteTask(id)
      notifySuccess(`已删除任务 #${String(id).padStart(6, '0')}`)
      if (items.value.length === 1 && page.value > 1) page.value -= 1
      await fetch()
      return true
    } catch (err) {
      notifyApiError(err)
      return false
    }
  }

  async function loadCounts(id: number): Promise<TaskCountsOut | null> {
    try {
      return await getTaskCounts(id)
    } catch (err) {
      notifyApiError(err)
      return null
    }
  }

  /**
   * 清空列表与筛选条件。**换账号时必须调用**（见 stores/user.ts）：
   * 同一个浏览器里 A 退出、B 登录时，如果不清，B 会先看到 A 的历史列表，
   * 直到下一次 fetch 回来才被替换 —— 那一秒里显示的是别人的数据。
   *
   * `requestSeq += 1` 让**在途**的请求作废：否则 A 发起的 fetch 在 B 登录后
   * 才返回，会把 A 的数据写进 B 的界面。
   */
  function reset(): void {
    requestSeq += 1
    items.value = []
    total.value = 0
    page.value = 1
    pageSize.value = 20
    loading.value = false
    error.value = null
    filters.value = { mode: '', status: '', dateRange: null }
  }

  async function loadDetections(
    id: number,
    query: { page?: number; page_size?: number; frame_index?: number; class_name?: string }
  ): Promise<PageOut<Detection> | null> {
    try {
      return await listTaskDetections(id, query)
    } catch (err) {
      notifyApiError(err)
      return null
    }
  }

  return {
    items,
    total,
    page,
    pageSize,
    loading,
    error,
    filters,
    fetch,
    setPage,
    setPageSize,
    applyFilters,
    resetFilters,
    reset,
    remove,
    loadCounts,
    loadDetections
  }
})
