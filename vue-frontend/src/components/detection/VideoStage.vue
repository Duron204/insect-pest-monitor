<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElButton, ElProgress } from 'element-plus'
import EmptyState from '@/components/common/EmptyState.vue'
import StatCard from '@/components/common/StatCard.vue'
import { formatDurationMs, formatNumber, formatTaskNo } from '@/utils'
import type { TaskDetailOut } from '@/types'

const props = withDefaults(
  defineProps<{
    /** 后端返回的标注视频地址（直接使用，前端不拼路径） */
    resultVideoUrl: string
    sourceVideoUrl: string
    taskId: number | null
    sourceName: string
    status: string
    progress: number
    progressLabel: string
    totalFrames: number
    processedFrames: number
    totalObjects: number
    uniqueObjects: number
    durationMs: number | null
    error: string | null
    task: TaskDetailOut | null
  }>(),
  {
    durationMs: null
  }
)

const emit = defineEmits<{
  (event: 'pick-video'): void
  (event: 'retry'): void
}>()

const view = ref<'annotated' | 'source'>('annotated')
const videoError = ref<string | null>(null)

const hasResult = computed(() => props.resultVideoUrl.length > 0)
const percent = computed(() => Math.round(Math.min(1, Math.max(0, props.progress)) * 100))

const statusText = computed(() => {
  switch (props.status) {
    case 'pending':
      return '排队中'
    case 'running':
      return '推理中'
    case 'done':
      return '已完成'
    case 'failed':
      return '失败'
    default:
      return props.progressLabel || '空闲'
  }
})

const toneClass = computed(() => {
  switch (props.status) {
    case 'done':
      return 'ip-dot ip-dot--ok'
    case 'running':
    case 'pending':
      return 'ip-dot ip-dot--warn'
    case 'failed':
      return 'ip-dot ip-dot--err'
    default:
      return 'ip-dot ip-dot--off'
  }
})

const currentSrc = computed(() =>
  view.value === 'annotated' ? props.resultVideoUrl : props.sourceVideoUrl
)

function switchView(next: 'annotated' | 'source'): void {
  if (view.value === next) return
  videoError.value = null
  view.value = next
}

function onError(): void {
  videoError.value = `无法播放${
    view.value === 'annotated' ? '标注视频' : '源视频'
  }，可能后台仍在生成或文件已被清理`
}
</script>

<template>
  <div class="video-stage ip-panel">
    <header class="video-stage__head">
      <div class="video-stage__title-group">
        <h2 class="ip-section-title">视频检测</h2>
        <span class="video-stage__task num">
          {{ taskId === null ? '未创建任务' : `任务 ${formatTaskNo(taskId)}` }}
        </span>
        <span class="video-stage__source ip-truncate" :title="sourceName">
          {{ sourceName || '—' }}
        </span>
      </div>
      <div class="video-stage__status">
        <span :class="toneClass" aria-hidden="true" />
        <span>{{ statusText }}</span>
      </div>
    </header>

    <div v-if="taskId === null" class="video-stage__empty">
      <EmptyState
        description="还没有视频检测任务"
        hint="选择本地视频（后端会按 stride 抽帧后台推理，完成后可播放标注视频）"
        action-text="选择视频文件"
        @action="emit('pick-video')"
      />
    </div>

    <template v-else>
      <div class="video-stage__viewer">
        <video
          v-if="currentSrc"
          :key="currentSrc"
          class="video-stage__video"
          :src="currentSrc"
          controls
          playsinline
          preload="metadata"
          @error="onError"
        />
        <div v-else class="video-stage__waiting">
          <span class="video-stage__spinner" aria-hidden="true" />
          {{ progressLabel || '正在生成标注视频…' }}
        </div>
      </div>

      <p v-if="videoError" class="video-stage__error">{{ videoError }}</p>

      <div class="video-stage__progress">
        <ElProgress
          :percentage="percent"
          :stroke-width="6"
          :show-text="false"
          :status="status === 'failed' ? 'exception' : status === 'done' ? 'success' : undefined"
        />
        <div class="video-stage__progress-row">
          <span class="num">
            {{ formatNumber(processedFrames) }} / {{ formatNumber(totalFrames) }} 帧
          </span>
          <span class="num">{{ percent }}%</span>
          <span class="video-stage__progress-label">{{ progressLabel }}</span>
        </div>
      </div>

      <div class="video-stage__stats">
        <StatCard label="去重虫数" :value="uniqueObjects" emphasis hint="按轨迹去重" />
        <StatCard label="框总数" :value="totalObjects" hint="全部已处理帧" />
        <StatCard label="处理耗时" :value="formatDurationMs(durationMs)" />
        <StatCard label="视频总帧" :value="totalFrames" :hint="task?.source_name ?? ''" />
      </div>

      <div class="video-stage__actions">
        <div class="video-stage__switch" role="group" aria-label="视频视图切换">
          <button
            class="video-stage__switch-item"
            :class="{ 'is-active': view === 'annotated' }"
            type="button"
            :aria-pressed="view === 'annotated'"
            aria-label="查看标注视频"
            :disabled="!hasResult"
            @click="switchView('annotated')"
          >
            标注结果
          </button>
          <button
            class="video-stage__switch-item"
            :class="{ 'is-active': view === 'source' }"
            type="button"
            :aria-pressed="view === 'source'"
            aria-label="查看源视频"
            :disabled="!sourceVideoUrl"
            @click="switchView('source')"
          >
            源视频
          </button>
        </div>
        <ElButton
          size="small"
          :disabled="status === 'running' || status === 'pending'"
          aria-label="重新选择视频"
          @click="emit('pick-video')"
        >
          重新选择
        </ElButton>
      </div>

      <p v-if="error" class="video-stage__error">{{ error }}</p>
    </template>
  </div>
</template>

<style scoped lang="scss">
.video-stage {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  min-height: 0;
  flex: 1 1 auto;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  &__title-group {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  &__task {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__source {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    max-width: 240px;
  }

  &__status {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
    flex: 0 0 auto;
  }

  &__empty {
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 260px;
  }

  &__viewer {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 260px;
    background: var(--ip-bg-deep);
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
    overflow: hidden;
  }

  &__video {
    width: 100%;
    max-height: 52vh;
    background: #000;
  }

  &__waiting {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__spinner {
    width: 14px;
    height: 14px;
    border: 2px solid var(--ip-line-strong);
    border-top-color: var(--ip-primary);
    border-radius: 50%;
    animation: video-spin 720ms linear infinite;
  }

  &__progress {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  &__progress-row {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__progress-label {
    margin-left: auto;
  }

  &__stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
    gap: 8px;
  }

  &__actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  &__switch {
    display: inline-flex;
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
    overflow: hidden;
  }

  &__switch-item {
    padding: 4px 12px;
    font-size: var(--ip-fs-xs);
    background: transparent;
    border: none;
    color: var(--ip-text-2);
    cursor: pointer;
  }

  &__switch-item.is-active {
    background: var(--ip-primary-weak);
    color: var(--ip-text);
  }

  &__switch-item:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  &__error {
    font-size: var(--ip-fs-xs);
    color: var(--ip-danger);
  }
}

@keyframes video-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
