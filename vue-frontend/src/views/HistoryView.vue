<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton } from 'element-plus'
import TaskDetailDrawer from '@/components/history/TaskDetailDrawer.vue'
import TaskFilters from '@/components/history/TaskFilters.vue'
import TaskList from '@/components/history/TaskList.vue'
import { downloadReport } from '@/api/tasks'
import type { ReportFormat } from '@/api/tasks'
import { useHistoryStore } from '@/stores/history'
import { confirmAction, notifyApiError, notifyInfo, notifySuccess } from '@/utils/notify'
import type { TaskOut } from '@/types'

const history = useHistoryStore()
const route = useRoute()
const router = useRouter()

const drawerVisible = ref(false)
const activeTask = ref<TaskOut | null>(null)
const exportingId = ref<number | null>(null)

const activeTaskId = computed(() => activeTask.value?.id ?? null)

function openTask(task: TaskOut): void {
  activeTask.value = task
  drawerVisible.value = true
  void router.replace({ query: { ...route.query, task: String(task.id) } })
}

function closeDrawer(): void {
  drawerVisible.value = false
  const query = { ...route.query }
  delete query.task
  void router.replace({ query })
}

async function handleDelete(task: TaskOut): Promise<void> {
  const ok = await confirmAction({
    title: '删除任务',
    message: `确认删除任务 #${String(task.id).padStart(6, '0')}？\n标注图与检测明细会一并移除，此操作不可撤销。`,
    confirmText: '删除',
    danger: true
  })
  if (!ok) return
  const removed = await history.remove(task.id)
  if (!removed) return
  if (activeTask.value?.id === task.id) closeDrawer()
}

async function handleDrawerDeleted(id: number): Promise<void> {
  const target = history.items.find((item) => item.id === id) ?? activeTask.value
  if (!target) return
  drawerVisible.value = false
  await handleDelete(target)
}

async function handleExport(payload: { task: TaskOut; format: ReportFormat }): Promise<void> {
  exportingId.value = payload.task.id
  try {
    const filename = await downloadReport(payload.task.id, payload.format)
    notifySuccess(`已导出 ${filename}`)
  } catch (err) {
    notifyApiError(err, false)
  } finally {
    exportingId.value = null
  }
}

async function handleEmptyAction(): Promise<void> {
  await router.push({ name: 'detect' })
}

/** 从 URL 的 ?task= 打开详情（检测台跳转过来时带） */
async function syncFromQuery(): Promise<void> {
  const value = route.query.task
  const id = typeof value === 'string' ? Number.parseInt(value, 10) : Number.NaN
  if (!Number.isFinite(id)) return
  const existing = history.items.find((item) => item.id === id)
  if (existing) {
    activeTask.value = existing
    drawerVisible.value = true
    return
  }
  if (history.total > 0) {
    notifyInfo(`任务 #${String(id).padStart(6, '0')} 不在当前筛选页，请调整筛选后重试`)
  }
}

onMounted(async () => {
  if (!history.items.length) await history.fetch()
  await syncFromQuery()
})

watch(
  () => route.query.task,
  () => {
    void syncFromQuery()
  }
)

const exporting = computed(() => exportingId.value !== null)
</script>

<template>
  <div class="history-view">
    <TaskFilters
      :mode="history.filters.mode"
      :status="history.filters.status"
      :date-range="history.filters.dateRange"
      :loading="history.loading"
      :total="history.total"
      @apply="history.applyFilters"
      @reset="history.resetFilters"
    />

    <div class="history-view__bar">
      <div class="history-view__bar-left">
        <ElButton
          size="small"
          :loading="history.loading"
          aria-label="刷新任务列表"
          @click="history.fetch()"
        >
          刷新
        </ElButton>
        <span class="history-view__hint">
          单击行打开详情抽屉：分类计数 / 框级明细分页 / 标注图或视频 / 报告导出
        </span>
      </div>
      <span v-if="exporting" class="history-view__exporting">正在生成报告…</span>
    </div>

    <TaskList
      :items="history.items"
      :total="history.total"
      :page="history.page"
      :page-size="history.pageSize"
      :loading="history.loading"
      :active-task-id="activeTaskId"
      @open="openTask"
      @delete="handleDelete"
      @export="handleExport"
      @page="history.setPage"
      @page-size="history.setPageSize"
      @empty-action="handleEmptyAction"
    />

    <TaskDetailDrawer
      v-model="drawerVisible"
      :task-id="activeTaskId"
      :summary="activeTask"
      @deleted="handleDrawerDeleted"
    />
  </div>
</template>

<style scoped lang="scss">
.history-view {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;

  &__bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  &__bar-left {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  &__hint {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__exporting {
    font-size: var(--ip-fs-xs);
    color: var(--ip-primary-hover);
  }
}
</style>
