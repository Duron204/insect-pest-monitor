<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElAlert, ElButton, ElOption, ElProgress, ElSelect, ElTooltip } from 'element-plus'
import { useRouter } from 'vue-router'
import CameraPanel from '@/components/detection/CameraPanel.vue'
import DetectionCanvas from '@/components/detection/DetectionCanvas.vue'
import DetectionTable from '@/components/detection/DetectionTable.vue'
import ModeBar from '@/components/detection/ModeBar.vue'
import ParamPanel from '@/components/detection/ParamPanel.vue'
import RealtimeStats from '@/components/detection/RealtimeStats.vue'
import VideoStage from '@/components/detection/VideoStage.vue'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import { useCameraCapture } from '@/composables/useCameraCapture'
import { useDetectionSocket } from '@/composables/useDetectionSocket'
import { useModelsStore } from '@/stores/models'
import { useSessionStore, type DetectMode } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { formatNumber, formatTaskNo } from '@/utils'
import { notifyInfo, notifyWarning } from '@/utils/notify'
import type { BatchItemOut, DetectionRow } from '@/types'

const session = useSessionStore()
const models = useModelsStore()
const ui = useUiStore()
const router = useRouter()

/* ------------------------------ 文件选择 ------------------------------ */

const imageInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
const videoInput = ref<HTMLInputElement | null>(null)

function openImagePicker(): void {
  imageInput.value?.click()
}

function openFolderPicker(): void {
  folderInput.value?.click()
}

function openVideoPicker(): void {
  videoInput.value?.click()
}

function isImageFile(file: File): boolean {
  if (file.type.startsWith('image/')) return true
  return /\.(jpe?g|png|bmp|webp|tif?f)$/i.test(file.name)
}

function onImagePicked(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  const file = files[0]
  if (!file) return
  void handleImage(file)
}

function onFolderPicked(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? []).filter((file) => isImageFile(file))
  input.value = ''
  if (files.length === 0) {
    notifyWarning('所选文件夹里没有找到图像文件')
    return
  }
  void handleFolder(files)
}

function onVideoPicked(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  const file = files[0]
  if (!file) return
  void handleVideo(file)
}

/* ------------------------------ 模式派发 ------------------------------ */

const cameraRequested = ref(false)

async function handleImage(file: File): Promise<void> {
  stopCamera()
  session.switchMode('image')
  await session.runImage(file)
}

async function handleFolder(files: File[]): Promise<void> {
  stopCamera()
  session.switchMode('batch')
  await session.runBatch(files)
}

async function handleVideo(file: File): Promise<void> {
  stopCamera()
  session.switchMode('video')
  await session.runVideo(file)
}

function onModeChange(mode: DetectMode): void {
  if (mode === session.mode) return
  if (mode === 'image' || mode === 'batch' || mode === 'video') {
    stopCamera()
    session.switchMode(mode)
  }
}

function onCameraRequested(): void {
  cameraRequested.value = true
  session.switchMode('camera')
}

/* ------------------------------ 摄像头 + WS ------------------------------ */

const pendingFrame = ref(false)
const sentFrames = ref(0)
const cameraBaseWidth = ref(0)
const cameraBaseHeight = ref(0)
const cameraElapsed = ref(0)

/* 先声明引用，避免在自身 options 里直接引用尚未初始化的 const（TDZ） */
let socketHandle: ReturnType<typeof useDetectionSocket> | null = null

const socket = useDetectionSocket({
  onReady: () => {
    socketHandle?.updateParams(session.conf, session.iou)
  },
  onTaskStarted: (sessionId) => {
    session.setLiveSession(sessionId)
  },
  onFrameResult: (payload) => {
    pendingFrame.value = false
    session.applyLiveFrame(payload)
  },
  onParamsUpdated: (params) => {
    if (params) {
      session.setConf(params.conf)
      session.setIou(params.iou)
    }
  },
  onError: () => {
    pendingFrame.value = false
  },
  onStateChange: (state) => {
    ui.setLiveSocketState(state)
  }
})

socketHandle = socket

const camera = useCameraCapture({
  fps: session.targetFps,
  quality: session.jpegQuality,
  maxEdge: 960,
  onError: (message) => {
    notifyWarning(message)
  },
  onStarted: (settings) => {
    cameraBaseWidth.value = settings.width ?? 0
    cameraBaseHeight.value = settings.height ?? 0
  },
  onStopped: () => {
    session.resetLiveState()
  }
})

const {
  stateLabel: socketLabel,
  stateTone: socketTone,
  isReady: socketReady,
  isActive: socketActive,
  retryCount: socketRetryCount,
  lastError: socketLastError,
  busy: socketBusy
} = socket

const {
  videoRef: cameraVideoRef,
  active: cameraActive,
  starting: cameraStartingRaw,
  supported: cameraSupported,
  error: cameraError,
  actualFps: cameraActualFps,
  frameSize: cameraFrameSize
} = camera

const cameraStarting = computed(() => cameraStartingRaw.value || (cameraActive.value && !socketReady.value))

const cameraHint = computed(() => {
  if (socketBusy.value) {
    return '后端实时通道已被占用（单实例设计：同时只允许 1 路流），请关闭其它检测页面后重试'
  }
  if (socketLabel.value === '重连中' || socketRetryCount.value > 0) {
    return `连接中断，正在重连（第 ${socketRetryCount.value} 次）…`
  }
  if (socketLastError.value && !socketReady.value) return socketLastError.value
  return ''
})

let sendTimer: number | null = null
let elapsedTimer: number | null = null
let paramTimer: number | null = null

function sendLoop(): void {
  if (!socketReady.value) return
  if (pendingFrame.value) return
  const base64 = camera.grabFrame()
  if (!base64) return
  const size = cameraFrameSize.value
  if (size) {
    cameraBaseWidth.value = size.width
    cameraBaseHeight.value = size.height
  }
  const seq = sentFrames.value + 1
  if (socket.sendFrame(base64, seq)) {
    sentFrames.value = seq
    pendingFrame.value = true
  }
}

function startSendLoop(): void {
  stopSendLoop()
  if (typeof window === 'undefined') return
  const interval = Math.max(33, Math.round(1000 / Math.max(1, session.targetFps)))
  sendTimer = window.setInterval(sendLoop, interval)
  elapsedTimer = window.setInterval(() => {
    cameraElapsed.value += 1
  }, 1000)
}

function stopSendLoop(): void {
  if (sendTimer !== null) {
    window.clearInterval(sendTimer)
    sendTimer = null
  }
  if (elapsedTimer !== null) {
    window.clearInterval(elapsedTimer)
    elapsedTimer = null
  }
  pendingFrame.value = false
}

async function startCamera(): Promise<void> {
  cameraRequested.value = true
  session.resetLiveState()
  sentFrames.value = 0
  cameraElapsed.value = 0
  session.switchMode('camera')
  if (!socket.isActive.value) socket.open()
  const started = await camera.start()
  if (!started) return
  const size = cameraFrameSize.value
  if (size) {
    cameraBaseWidth.value = size.width
    cameraBaseHeight.value = size.height
  }
  startSendLoop()
  notifyInfo('摄像头已开启，画面正通过实时通道检测')
}

function stopCamera(): void {
  cameraRequested.value = false
  stopSendLoop()
  if (cameraActive.value) void camera.stop()
  if (socket.isActive.value) socket.close()
  session.resetLiveState()
}

function onCameraToggle(): void {
  if (cameraActive.value || socket.isActive.value) stopCamera()
  else void startCamera()
}

/* ------------------------------ 参数联动 ------------------------------ */

watch(
  () => [session.conf, session.iou] as const,
  ([conf, iou]) => {
    if (!socketReady.value) return
    if (paramTimer !== null) window.clearTimeout(paramTimer)
    paramTimer = window.setTimeout(() => {
      paramTimer = null
      socket.updateParams(conf, iou)
    }, 220)
  }
)

watch(
  () => session.targetFps,
  (fps) => {
    camera.setFps(fps)
    if (cameraActive.value) startSendLoop()
  }
)

watch(
  () => session.jpegQuality,
  (quality) => camera.setQuality(quality)
)

function resetParams(): void {
  session.setConf(0.25)
  session.setIou(0.45)
  session.setStride(2)
  session.setTargetFps(10)
}

function onQualityChange(value: number): void {
  session.jpegQuality = Math.min(0.95, Math.max(0.3, value))
}

/* ------------------------------ 选中联动 ------------------------------ */

function onCanvasSelect(key: string | null): void {
  session.selectRow(key)
}

function onCanvasHover(key: string | null): void {
  session.hoverRow(key)
}

function onTableSelect(row: DetectionRow | null): void {
  session.selectRow(row ? row.key : null)
}

function onTableHover(row: DetectionRow | null): void {
  session.hoverRow(row ? row.key : null)
}

const selectedRow = computed(() => session.rowDetectionsOf(session.selectedRowKey))

/** 「画面最高分」—— 取**当前这一帧**，不是整个批次（批次口径在表格表头已给出）。 */
const maxScore = computed<number | null>(() => {
  const rows = session.activeFrameRows
  if (rows.length === 0) return null
  return rows.reduce((acc, row) => Math.max(acc, row.score), 0)
})

/* ------------------------------ 批量导航 ------------------------------ */

const batchItems = computed<BatchItemOut[]>(() => session.batchResult?.items ?? [])
const batchFailed = computed<BatchItemOut[]>(() =>
  batchItems.value.filter((item) => item.error !== null && item.error !== undefined)
)

function stepBatch(delta: number): void {
  session.stepBatch(delta)
}

function goBatchIndex(index: number): void {
  session.selectBatchIndex(index)
}

const batchFailureHint = computed(() =>
  batchFailed.value.length === 0
    ? ''
    : `${batchFailed.value.length} 张图像处理失败，可切换查看或稍后重试`
)

function failureText(error: string | null): string {
  return error && error.length > 0 ? error : '未知错误'
}

/* ------------------------------ 结果汇总 ------------------------------ */

const activeCountText = computed(() => {
  const entries = Object.entries(session.activeCounts)
  if (entries.length === 0) return '—'
  return entries
    .sort((a, b) => b[1] - a[1])
    .map(([name, count]) => `${name} ${count}`)
    .join(' · ')
})

const modeLabel = computed(() => {
  switch (session.mode) {
    case 'image':
      return '单图检测'
    case 'batch':
      return '批量检测'
    case 'video':
      return '视频检测'
    default:
      return '摄像头实时'
  }
})

const currentModelName = computed(() => models.currentName)

const detectError = computed(() => ({ message: session.errorMessage }))

function clearDetectError(): void {
  session.errorMessage = null
}

async function goHistory(): Promise<void> {
  await router.push({ name: 'history', query: { task: String(session.currentTaskId ?? '') } })
}

/* ------------------------------ 生命周期 ------------------------------ */

onMounted(() => {
  void models.loadRegistry()
  if (session.mode === 'camera') session.switchMode('image')
})

onBeforeUnmount(() => {
  stopCamera()
  stopSendLoop()
  if (paramTimer !== null) window.clearTimeout(paramTimer)
  ui.setLiveSocketState('idle')
  session.dispose()
})

watch(
  () => socketReady.value,
  (ready) => {
    if (ready) socket.updateParams(session.conf, session.iou)
  }
)

watch(
  () => session.mode,
  (mode) => {
    if (mode !== 'camera' && (cameraActive.value || socket.isActive.value)) stopCamera()
  }
)
</script>

<template>
  <div class="detect">
    <input
      ref="imageInput"
      class="detect__input"
      type="file"
      accept="image/*"
      aria-label="选择图片文件"
      @change="onImagePicked"
    >
    <input
      ref="folderInput"
      class="detect__input"
      type="file"
      accept="image/*"
      multiple
      webkitdirectory
      directory
      aria-label="选择图像文件夹"
      @change="onFolderPicked"
    >
    <input
      ref="videoInput"
      class="detect__input"
      type="file"
      accept="video/*"
      aria-label="选择视频文件"
      @change="onVideoPicked"
    >

    <aside class="detect__left">
      <ModeBar
        :model-value="session.mode"
        :disabled="session.isRunning"
        :camera-supported="cameraSupported"
        @update:model-value="onModeChange"
        @pick-image="openImagePicker"
        @pick-folder="openFolderPicker"
        @pick-video="openVideoPicker"
        @camera="onCameraRequested"
      />

      <ParamPanel
        :conf="session.conf"
        :iou="session.iou"
        :stride="session.stride"
        :target-fps="session.targetFps"
        :show-stride="session.mode === 'video'"
        :show-fps="session.mode === 'camera'"
        :live-connected="socketReady"
        :disabled="session.isRunning"
        @update:conf="session.setConf"
        @update:iou="session.setIou"
        @update:stride="session.setStride"
        @update:target-fps="session.setTargetFps"
        @reset="resetParams"
      />

      <section class="detect__meta ip-panel" aria-label="当前任务信息">
        <h2 class="ip-section-title">当前任务</h2>
        <dl class="detect__meta-list">
          <div>
            <dt>模式</dt>
            <dd>{{ modeLabel }}</dd>
          </div>
          <div>
            <dt>画面标识</dt>
            <dd class="ip-truncate" :title="session.activeFrameLabel">
              {{ session.activeFrameLabel }}
            </dd>
          </div>
          <div>
            <dt>当前模型</dt>
            <dd class="ip-truncate" :title="currentModelName">{{ currentModelName }}</dd>
          </div>
          <div>
            <dt>本帧各类</dt>
            <dd class="ip-truncate" :title="activeCountText">{{ activeCountText }}</dd>
          </div>
        </dl>
        <ElButton
          v-if="session.currentTaskId !== null"
          text
          size="small"
          class="detect__history-link"
          aria-label="在历史记录中查看该任务"
          @click="goHistory"
        >
          在历史记录中查看 →
        </ElButton>
        <p v-else class="detect__meta-hint">尚未产生任务</p>
      </section>
    </aside>

    <section class="detect__center">
      <ElProgress
        v-if="session.isRunning"
        class="detect__progress"
        :percentage="Math.round(session.progress * 100)"
        :stroke-width="4"
        :show-text="false"
      />
      <p v-if="session.progressLabel" class="detect__progress-label">
        {{ session.progressLabel }}
      </p>

      <DetectionCanvas
        v-if="session.mode === 'image' || session.mode === 'batch'"
        :image-url="session.previewUrl"
        :rows="session.activeFrameRows"
        :hovered-key="session.hoveredRowKey"
        :selected-key="session.selectedRowKey"
        :frame-label="session.activeFrameLabel"
        :loading="session.isRunning"
        :empty-title="session.mode === 'batch' ? '尚未选择图像文件夹' : '尚未选择检测图片'"
        :empty-hint="
          session.mode === 'batch'
            ? '选择文件夹后一次性上传（后端单次最多 50 张），可用下方导航逐张查看'
            : '选择一张田间图像，检测结果会以 canvas 画框叠加显示'
        "
        empty-action-text="选择图像"
        @select="onCanvasSelect"
        @hover="onCanvasHover"
        @empty-action="openImagePicker"
      />

      <DetectionCanvas
        v-else-if="session.mode === 'camera'"
        image-url=""
        overlay-only
        :base-width="cameraBaseWidth"
        :base-height="cameraBaseHeight"
        :rows="session.liveRows"
        :hovered-key="session.hoveredRowKey"
        :selected-key="session.selectedRowKey"
        :frame-label="session.liveFrameLabel || '摄像头实时画面'"
        empty-title="摄像头尚未开启"
        empty-hint="点击下方按钮后浏览器会请求摄像头权限（需 HTTPS 或 localhost）"
        empty-action-text="开启摄像头"
        @select="onCanvasSelect"
        @hover="onCanvasHover"
        @empty-action="onCameraToggle"
      >
        <template #base>
          <video
            ref="cameraVideoRef"
            class="detect__video"
            :class="{ 'is-hidden': !cameraActive }"
            playsinline
            muted
            aria-label="摄像头实时预览"
          />
        </template>
      </DetectionCanvas>

      <VideoStage
        v-else
        :result-video-url="session.videoTask?.result_video_path ?? ''"
        :source-video-url="session.videoSourceObjectUrl ?? ''"
        :task-id="session.videoAccepted?.task_id ?? null"
        :source-name="session.videoAccepted?.source_name ?? ''"
        :status="session.videoTask?.status ?? (session.videoAccepted ? 'running' : 'idle')"
        :progress="session.progress"
        :progress-label="session.progressLabel"
        :total-frames="session.videoAccepted?.total_frames ?? 0"
        :processed-frames="session.videoTask?.frame_count ?? 0"
        :total-objects="session.videoTask?.total_objects ?? 0"
        :unique-objects="session.videoTask?.unique_objects ?? 0"
        :duration-ms="session.videoTask?.duration_ms ?? null"
        :error="session.videoTask?.error ?? session.errorMessage"
        :task="session.videoTask"
        @pick-video="openVideoPicker"
      />

      <div
        v-if="session.mode === 'batch' && batchItems.length > 0"
        class="detect__batchbar ip-panel"
      >
        <div class="detect__batchbar-left">
          <ElButton
            size="small"
            :disabled="session.batchIndex === 0"
            aria-label="上一张图像"
            @click="stepBatch(-1)"
          >
            上一张
          </ElButton>
          <ElButton
            size="small"
            :disabled="session.batchIndex >= batchItems.length - 1"
            aria-label="下一张图像"
            @click="stepBatch(1)"
          >
            下一张
          </ElButton>
          <span class="num detect__batchbar-index">
            {{ session.batchIndex + 1 }} / {{ batchItems.length }}
          </span>
          <span
            class="detect__batchbar-name ip-truncate"
            :title="session.activeBatchItem?.filename ?? ''"
          >
            {{ session.activeBatchItem?.filename ?? '—' }}
          </span>
        </div>
        <ElSelect
          :model-value="session.batchIndex"
          size="small"
          class="detect__batchbar-select"
          placeholder="跳转到图像"
          aria-label="跳转到指定图像"
          @update:model-value="goBatchIndex"
        >
          <ElOption
            v-for="(item, index) in batchItems"
            :key="`${index}-${item.filename}`"
            :label="`${index + 1}. ${item.filename}${item.error ? '（失败）' : ''}`"
            :value="index"
          />
        </ElSelect>
      </div>

      <ElAlert
        v-if="batchFailureHint"
        class="detect__alert"
        :title="batchFailureHint"
        type="warning"
        :closable="false"
        show-icon
      >
        <ul class="detect__fail-list">
          <li v-for="item in batchFailed" :key="item.filename">
            <span class="ip-truncate">{{ item.filename }}</span>
            <span class="detect__fail-reason">{{ failureText(item.error) }}</span>
          </li>
        </ul>
      </ElAlert>

      <DetectionTable
        v-if="session.mode !== 'video'"
        :rows="session.activeRows"
        :selected-key="session.selectedRowKey"
        :hovered-key="session.hoveredRowKey"
        :loading="session.isRunning"
        :show-frame-column="session.mode !== 'image'"
        :height="232"
        :max-score="maxScore"
        :empty-text="
          session.mode === 'camera'
            ? '实时流尚无检出目标'
            : '当前画面没有检测到目标，可尝试降低置信度阈值'
        "
        @select="onTableSelect"
        @hover="onTableHover"
      />
    </section>

    <aside class="detect__right">
      <CameraPanel
        v-if="session.mode === 'camera'"
        :capturing="cameraActive"
        :starting="cameraStarting"
        :supported="cameraSupported"
        :error="cameraError"
        :socket-label="socketLabel"
        :socket-tone="socketTone"
        :actual-fps="cameraActualFps"
        :sent-frames="sentFrames"
        :dropped-hint="cameraHint"
        :unique-objects="session.liveUniqueObjects"
        :total-objects="session.liveCumulativeObjects"
        :active-tracks="session.liveActiveTracks"
        :elapsed-seconds="cameraElapsed || session.liveElapsedSeconds"
        :target-fps="session.targetFps"
        :quality="session.jpegQuality"
        :selected-model-name="currentModelName"
        @start="startCamera"
        @stop="stopCamera"
        @update:target-fps="session.setTargetFps"
        @update:quality="onQualityChange"
      />

      <RealtimeStats
        :frame-counts="session.activeCounts"
        :total-objects="session.totalObjects"
        :unique-objects="session.uniqueObjects"
        :inference-ms="session.inferenceMs"
        :frame-total="session.frameTotal"
        :frame-index="session.liveFrameIndex"
        :active-tracks="session.liveActiveTracks"
        :elapsed-seconds="session.liveElapsedSeconds"
        :loading="session.isRunning"
        :live="session.mode === 'camera'"
        :mode="session.mode"
      />

      <section v-if="selectedRow" class="detect__picked ip-panel" aria-label="选中目标详情">
        <header class="detect__picked-head">
          <h2 class="ip-section-title">选中目标</h2>
          <ElButton text size="small" aria-label="取消选中目标" @click="session.selectRow(null)">
            取消
          </ElButton>
        </header>
        <dl class="detect__meta-list">
          <div>
            <dt>中文名</dt>
            <dd>{{ selectedRow.zhName }}</dd>
          </div>
          <div>
            <dt>英文名</dt>
            <dd>{{ selectedRow.className }}</dd>
          </div>
          <div>
            <dt>置信度</dt>
            <dd class="num">{{ selectedRow.score.toFixed(3) }}</dd>
          </div>
          <div>
            <dt>边界框</dt>
            <dd class="num">{{ selectedRow.bbox.map((value) => value.toFixed(0)).join(', ') }}</dd>
          </div>
          <div>
            <dt>轨迹 ID</dt>
            <dd class="num">
              {{ selectedRow.trackId === null ? '—' : `#${selectedRow.trackId}` }}
            </dd>
          </div>
          <div>
            <dt>画面</dt>
            <dd class="ip-truncate" :title="selectedRow.frameLabel">{{ selectedRow.frameLabel }}</dd>
          </div>
        </dl>
      </section>

      <ElTooltip content="当前画面检测记录条数" placement="left">
        <p class="detect__count num">
          当前画面记录 {{ formatNumber(session.activeFrameRows.length) }} 条
        </p>
      </ElTooltip>
    </aside>

    <ErrorBanner
      class="detect__banner"
      :message="detectError.message"
      title="检测失败"
      @close="clearDetectError"
    />
  </div>
</template>

<style scoped lang="scss">
.detect {
  display: grid;
  grid-template-columns: 236px minmax(0, 1fr) 268px;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;

  &__input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
    pointer-events: none;
  }

  &__left,
  &__right {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-height: 0;
    overflow-y: auto;
  }

  &__center {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-height: 0;
    min-width: 0;
  }

  &__progress {
    flex: 0 0 auto;
  }

  &__progress-label {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__video {
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: #000;
  }

  &__video.is-hidden {
    visibility: hidden;
  }

  &__meta {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
  }

  &__meta-list {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin: 0;

    div {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 10px;
    }

    dt {
      flex: 0 0 auto;
      font-size: var(--ip-fs-xs);
      color: var(--ip-text-3);
    }

    dd {
      margin: 0;
      min-width: 0;
      font-size: var(--ip-fs-xs);
      color: var(--ip-text);
      text-align: right;
    }
  }

  &__meta-hint {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__history-link {
    align-self: flex-start;
    padding: 0;
    font-size: var(--ip-fs-xs);
  }

  &__batchbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 6px 10px;
    flex: 0 0 auto;
  }

  &__batchbar-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  &__batchbar-index {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__batchbar-name {
    max-width: 260px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__batchbar-select {
    width: 220px;
    flex: 0 0 auto;
  }

  &__alert {
    flex: 0 0 auto;
  }

  &__fail-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin-top: 4px;

    li {
      display: flex;
      gap: 8px;
      font-size: var(--ip-fs-xs);
    }
  }

  &__fail-reason {
    color: var(--ip-warn);
  }

  &__picked {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
  }

  &__picked-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__count {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    text-align: center;
  }

  &__banner {
    grid-column: 1 / -1;
  }
}

@media (max-width: 1439px) {
  .detect {
    grid-template-columns: 220px minmax(0, 1fr);
  }

  /* 1024–1440：右栏改成抽屉（默认收起，点击浮动按钮展开） */
  .detect__right {
    position: fixed;
    right: 0;
    top: var(--ip-header-h);
    bottom: 0;
    width: 320px;
    z-index: 1500;
    padding: 10px;
    background: var(--ip-panel);
    border-left: 1px solid var(--ip-line);
    box-shadow: var(--ip-shadow-md);
    overflow-y: auto;
  }
}

@media (max-width: 1023px) {
  .detect {
    grid-template-columns: 1fr;
    overflow-y: auto;
    padding: 10px;
  }

  .detect__right {
    position: static;
    width: auto;
    box-shadow: none;
    border: none;
    background: transparent;
    padding: 0;
  }
}
</style>
