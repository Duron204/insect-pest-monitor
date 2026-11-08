<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  ElButton,
  ElDescriptions,
  ElDescriptionsItem,
  ElDrawer,
  ElImage,
  ElOption,
  ElPagination,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag
} from 'element-plus'
import EmptyState from '@/components/common/EmptyState.vue'
import { listTaskDetections, getTask, getTaskCounts } from '@/api/tasks'
import {
  formatDateTime,
  formatDurationMs,
  formatNumber,
  formatTaskNo,
  sortedCountEntries,
  taskModeLabel,
  taskStatusLabel
} from '@/utils'
import { notifyApiError } from '@/utils/notify'
import type { Detection, TaskCountsOut, TaskDetailOut, TaskOut } from '@/types'

const props = defineProps<{
  modelValue: boolean
  taskId: number | null
  /** 列表里已有的摘要信息，用于抽屉打开瞬间先渲染 */
  summary?: TaskOut | null
}>()

const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
  (event: 'deleted', id: number): void
}>()

const activeTab = ref<'overview' | 'detections' | 'media'>('overview')
const detail = ref<TaskDetailOut | null>(null)
const counts = ref<TaskCountsOut | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const detections = ref<Detection[]>([])
const detectionTotal = ref(0)
const detectionPage = ref(1)
const detectionPageSize = ref(20)
const detectionLoading = ref(false)
const classFilter = ref('')

const countEntries = computed(() => sortedCountEntries(counts.value?.counts ?? detail.value?.counts ?? {}))

const annotatedImageUrl = computed(() => {
  const path = detail.value?.result_image_path ?? counts.value?.result_image_path ?? null
  return path ?? ''
})

const resultVideoUrl = computed(() => {
  const path = detail.value?.result_video_path ?? counts.value?.result_video_path ?? null
  return path ?? ''
})

const classOptions = computed(() => countEntries.value.map((entry) => entry[0]))

async function loadDetail(id: number): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [task, taskCounts] = await Promise.all([getTask(id), getTaskCounts(id).catch(() => null)])
    detail.value = task
    counts.value = taskCounts
  } catch (err) {
    error.value = notifyApiError(err)
    detail.value = null
  } finally {
    loading.value = false
  }
}

async function loadDetections(id: number): Promise<void> {
  detectionLoading.value = true
  try {
    const response = await listTaskDetections(id, {
      page: detectionPage.value,
      page_size: detectionPageSize.value,
      class_name: classFilter.value || undefined
    })
    detections.value = response.items ?? []
    detectionTotal.value = response.total ?? 0
  } catch (err) {
    notifyApiError(err)
    detections.value = []
    detectionTotal.value = 0
  } finally {
    detectionLoading.value = false
  }
}

function resetState(): void {
  detail.value = null
  counts.value = null
  detections.value = []
  detectionTotal.value = 0
  detectionPage.value = 1
  classFilter.value = ''
  error.value = null
  activeTab.value = 'overview'
}

watch(
  () => [props.modelValue, props.taskId] as const,
  ([visible, id]) => {
    if (!visible) return
    resetState()
    if (id !== null) {
      void loadDetail(id)
      void loadDetections(id)
    }
  }
)

watch([detectionPage, detectionPageSize], () => {
  if (props.taskId !== null && props.modelValue) void loadDetections(props.taskId)
})

watch(classFilter, () => {
  detectionPage.value = 1
  if (props.taskId !== null && props.modelValue) void loadDetections(props.taskId)
})

function close(): void {
  emit('update:modelValue', false)
}

function onDelete(): void {
  if (props.taskId !== null) emit('deleted', props.taskId)
}

/** el-table 的插槽把 row 推断为 DefaultRow，传给强类型函数前先统一收窄。 */
const asDetection = (row: unknown): Detection => row as Detection

function bboxText(detection: Detection): string {
  const bbox = detection.bbox
  return `${bbox?.[0]?.toFixed(0) ?? '0'},${bbox?.[1]?.toFixed(0) ?? '0'} → ${bbox?.[2]?.toFixed(0) ?? '0'},${bbox?.[3]?.toFixed(0) ?? '0'}`
}

const summaryTask = computed(() => detail.value ?? props.summary ?? null)
</script>

<template>
  <ElDrawer
    :model-value="modelValue"
    :size="720"
    :with-header="false"
    append-to-body
    destroy-on-close
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="task-detail">
      <header class="task-detail__head">
        <div>
          <h2 class="task-detail__title">
            任务 {{ formatTaskNo(taskId) }}
            <ElTag
              v-if="summaryTask"
              size="small"
              effect="plain"
              :type="summaryTask.status === 'failed' ? 'danger' : summaryTask.status === 'done' ? 'success' : 'warning'"
            >
              {{ taskStatusLabel(summaryTask.status) }}
            </ElTag>
          </h2>
          <p class="task-detail__subtitle ip-truncate" :title="summaryTask?.source_name ?? ''">
            {{ summaryTask ? `${taskModeLabel(summaryTask.mode)} · ${summaryTask.source_name ?? '—'}` : '正在加载…' }}
          </p>
        </div>
        <div class="task-detail__head-actions">
          <ElButton size="small" text aria-label="删除该任务" type="danger" @click="onDelete">
            删除任务
          </ElButton>
          <ElButton size="small" text aria-label="关闭详情抽屉" @click="close">关闭</ElButton>
        </div>
      </header>

      <p v-if="error" class="task-detail__error">{{ error }}</p>

      <div class="task-detail__cards">
        <div class="task-detail__card task-detail__card--primary">
          <span class="task-detail__card-label">去重虫数</span>
          <span class="num task-detail__card-value">
            {{ formatNumber(counts?.unique_objects ?? summaryTask?.unique_objects ?? 0) }}
          </span>
        </div>
        <div class="task-detail__card">
          <span class="task-detail__card-label">框总数</span>
          <span class="num task-detail__card-value">
            {{ formatNumber(counts?.total_objects ?? summaryTask?.total_objects ?? 0) }}
          </span>
        </div>
        <div class="task-detail__card">
          <span class="task-detail__card-label">帧数</span>
          <span class="num task-detail__card-value">
            {{ formatNumber(detail?.frame_count ?? summaryTask?.frame_count ?? 0) }}
            <small v-if="detail?.total_frames"> / {{ formatNumber(detail.total_frames) }}</small>
          </span>
        </div>
        <div class="task-detail__card">
          <span class="task-detail__card-label">进度</span>
          <span class="num task-detail__card-value">
            {{ Math.round(counts?.progress ?? detail?.progress ?? 0) }}%
          </span>
        </div>
      </div>

      <ElTabs v-model="activeTab" class="task-detail__tabs">
        <ElTabPane label="概览" name="overview">
          <ElDescriptions :column="2" border size="small" class="task-detail__desc">
            <ElDescriptionsItem label="任务号">{{ formatTaskNo(taskId) }}</ElDescriptionsItem>
            <ElDescriptionsItem label="模式">
              {{ taskModeLabel(detail?.mode ?? summaryTask?.mode ?? null) }}
            </ElDescriptionsItem>
            <ElDescriptionsItem label="来源">
              <span class="ip-truncate">{{ detail?.source_name ?? summaryTask?.source_name ?? '—' }}</span>
            </ElDescriptionsItem>
            <ElDescriptionsItem label="状态">
              {{ taskStatusLabel(detail?.status ?? summaryTask?.status ?? null) }}
            </ElDescriptionsItem>
            <ElDescriptionsItem label="开始时间">
              {{ formatDateTime(detail?.started_at ?? summaryTask?.started_at ?? null) }}
            </ElDescriptionsItem>
            <ElDescriptionsItem label="结束时间">
              {{ formatDateTime(detail?.finished_at ?? summaryTask?.finished_at ?? null) }}
            </ElDescriptionsItem>
            <ElDescriptionsItem label="conf / IoU">
              <span class="num">
                {{ detail?.conf === null || detail?.conf === undefined ? '—' : detail.conf.toFixed(2) }}
                /
                {{ detail?.iou === null || detail?.iou === undefined ? '—' : detail.iou.toFixed(2) }}
              </span>
            </ElDescriptionsItem>
            <ElDescriptionsItem label="耗时">
              {{ formatDurationMs(detail?.duration_ms ?? summaryTask?.duration_ms ?? null) }}
            </ElDescriptionsItem>
            <ElDescriptionsItem label="模型 ID">
              <span class="num">{{ detail?.model_id ?? summaryTask?.model_id ?? '—' }}</span>
            </ElDescriptionsItem>
            <ElDescriptionsItem label="检测行数">
              <span class="num">{{ counts?.detection_rows ?? '—' }}</span>
            </ElDescriptionsItem>
            <ElDescriptionsItem label="错误信息" :span="2">
              <span :class="detail?.error ? 'task-detail__warn' : ''">
                {{ detail?.error ?? '无' }}
              </span>
            </ElDescriptionsItem>
          </ElDescriptions>

          <div class="task-detail__counts">
            <p class="ip-label">分类计数</p>
            <div v-if="countEntries.length > 0" class="task-detail__tags">
              <ElTag
                v-for="entry in countEntries"
                :key="entry[0]"
                size="small"
                effect="plain"
                class="task-detail__tag"
              >
                {{ entry[0] }} <span class="num">{{ entry[1] }}</span>
              </ElTag>
            </div>
            <EmptyState v-else compact description="该任务没有分类计数" hint="可能是任务失败或尚未产生结果" />
          </div>
        </ElTabPane>

        <ElTabPane :label="`框级明细${detectionTotal > 0 ? ` (${detectionTotal})` : ''}`" name="detections">
          <div class="task-detail__filter">
            <ElSelect
              v-model="classFilter"
              size="small"
              clearable
              placeholder="按中文名筛选"
              aria-label="按虫种筛选检测明细"
              class="task-detail__filter-select"
            >
              <ElOption v-for="name in classOptions" :key="name" :label="name" :value="name" />
            </ElSelect>
            <ElButton
              size="small"
              text
              :loading="detectionLoading"
              aria-label="刷新检测明细"
              @click="taskId !== null && loadDetections(taskId)"
            >
              刷新
            </ElButton>
          </div>

          <ElTable :data="detections" size="small" :height="330" v-loading="detectionLoading">
            <ElTableColumn label="中文名" min-width="110">
              <template #default="{ row }">{{ row.zh_name || '—' }}</template>
            </ElTableColumn>
            <ElTableColumn label="英文名" min-width="130">
              <template #default="{ row }">
                <span class="ip-truncate">{{ row.class_name }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="置信度" width="88" align="right">
              <template #default="{ row }">
                <span class="num">{{ row.score.toFixed(3) }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="边界框" min-width="180">
              <template #default="{ row }">
                <span class="num task-detail__bbox">{{ bboxText(asDetection(row)) }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="轨迹" width="72" align="right">
              <template #default="{ row }">
                <span class="num">{{ row.track_id === null ? '—' : `#${row.track_id}` }}</span>
              </template>
            </ElTableColumn>
            <template #empty>
              <EmptyState compact description="没有检测明细" hint="该任务的检测框记录为空" />
            </template>
          </ElTable>

          <div class="task-detail__pager">
            <ElPagination
              :current-page="detectionPage"
              :page-size="detectionPageSize"
              :total="detectionTotal"
              :page-sizes="[20, 50, 100]"
              size="small"
              layout="total, sizes, prev, pager, next"
              background
              @current-change="(value: number) => (detectionPage = value)"
              @size-change="(value: number) => (detectionPageSize = value)"
            />
          </div>
        </ElTabPane>

        <ElTabPane label="结果文件" name="media">
          <div class="task-detail__media">
            <template v-if="resultVideoUrl">
              <p class="ip-label">标注视频（后端 result_video_path）</p>
              <video class="task-detail__video" :src="resultVideoUrl" controls playsinline preload="metadata" />
            </template>
            <template v-else-if="annotatedImageUrl">
              <p class="ip-label">标注图像（后端 result_image_path）</p>
              <ElImage
                class="task-detail__image"
                :src="annotatedImageUrl"
                :preview-src-list="[annotatedImageUrl]"
                fit="contain"
                :initial-index="0"
                preview-teleported
                aria-label="标注结果图像"
              />
            </template>
            <EmptyState
              v-else
              compact
              description="该任务没有结果文件"
              hint="图片任务会生成标注图，视频任务完成后会生成标注视频"
            />
          </div>
        </ElTabPane>
      </ElTabs>
    </div>
  </ElDrawer>
</template>

<style scoped lang="scss">
.task-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  padding: 4px 2px;

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--ip-line);
  }

  &__title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 15px;
    font-weight: 600;
    font-family: var(--ip-font-mono);
  }

  &__subtitle {
    margin-top: 2px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    max-width: 420px;
  }

  &__head-actions {
    display: flex;
    align-items: center;
    gap: 4px;
    flex: 0 0 auto;
  }

  &__error {
    font-size: var(--ip-fs-xs);
    color: var(--ip-danger);
  }

  &__cards {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 8px;
  }

  &__card {
    display: flex;
    flex-direction: column;
    padding: 8px 10px;
    background: var(--ip-panel-2);
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
  }

  &__card--primary {
    border-color: var(--ip-primary-line);
    background: var(--ip-primary-weak);
  }

  &__card-label {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__card-value {
    font-size: 20px;
    font-weight: 600;
    color: var(--ip-text);
  }

  &__card--primary .task-detail__card-value {
    color: var(--ip-primary-hover);
  }

  &__card-value small {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__tabs {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
  }

  &__desc {
    margin-bottom: 12px;
  }

  &__warn {
    color: var(--ip-warn);
  }

  &__counts {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  &__tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  &__tag {
    font-family: var(--ip-font);
  }

  &__filter {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }

  &__filter-select {
    width: 190px;
  }

  &__bbox {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__pager {
    display: flex;
    justify-content: flex-end;
    margin-top: 8px;
  }

  &__media {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  &__video {
    width: 100%;
    max-height: 380px;
    background: #000;
    border-radius: var(--ip-radius-sm);
  }

  &__image {
    max-height: 420px;
  }
}
</style>
