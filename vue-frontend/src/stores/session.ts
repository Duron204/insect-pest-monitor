import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { BATCH_MAX_FILES, detectBatch, detectImage, detectVideo } from '@/api/detection'
import { getTask } from '@/api/tasks'
import { notifyApiError, notifySuccess, notifyWarning } from '@/utils/notify'
import { formatTaskNo } from '@/utils'
import type {
  BatchDetectionOut,
  BatchItemOut,
  CountMap,
  Detection,
  DetectionRow,
  ImageDetectionOut,
  TaskDetailOut
} from '@/types'

export type DetectMode = 'image' | 'batch' | 'video' | 'camera'
export type RunPhase = 'idle' | 'uploading' | 'running' | 'done' | 'error'

let rowSeq = 0

function nextRowKey(prefix: string): string {
  rowSeq += 1
  return `${prefix}-${rowSeq}`
}

function normalizeBbox(bbox: Detection['bbox']): [number, number, number, number] {
  return [bbox?.[0] ?? 0, bbox?.[1] ?? 0, bbox?.[2] ?? 0, bbox?.[3] ?? 0]
}

export function toDetectionRows(
  detections: Detection[] | null | undefined,
  frameLabel: string,
  frameIndex: number,
  prefix = 'row'
): DetectionRow[] {
  if (!detections || detections.length === 0) return []
  return detections.map((detection) => ({
    key: nextRowKey(prefix),
    frameLabel,
    frameIndex,
    classId: detection.class_id,
    className: detection.class_name,
    zhName: detection.zh_name,
    score: detection.score,
    bbox: normalizeBbox(detection.bbox),
    trackId: detection.track_id ?? null
  }))
}

export const useSessionStore = defineStore('session', () => {
  /* ------------------------------ 参数 ------------------------------ */

  const conf = ref(0.25)
  const iou = ref(0.45)
  const stride = ref(2)
  const targetFps = ref(10)
  const jpegQuality = ref(0.7)

  /* ------------------------------ 模式与状态 ------------------------------ */

  const mode = ref<DetectMode>('image')
  const phase = ref<RunPhase>('idle')
  const errorMessage = ref<string | null>(null)
  const progress = ref(0)
  const progressLabel = ref('')

  const abortController = ref<AbortController | null>(null)

  /* ------------------------------ 单图结果 ------------------------------ */

  const objectUrl = ref<string | null>(null)
  const localLabel = ref('')
  const imageResult = ref<ImageDetectionOut | null>(null)
  const imageRows = ref<DetectionRow[]>([])

  /* ------------------------------ 批量结果 ------------------------------ */

  const batchResult = ref<BatchDetectionOut | null>(null)
  const batchIndex = ref(0)
  const batchRows = ref<DetectionRow[]>([])

  /* ------------------------------ 视频结果 ------------------------------ */

  const videoAccepted = ref<{ task_id: number; source_name: string; total_frames: number } | null>(null)
  const videoTask = ref<TaskDetailOut | null>(null)
  const videoPollTimer = ref<number | null>(null)
  const videoSourceObjectUrl = ref<string | null>(null)

  /* ---------------------------- 摄像头实时 ---------------------------- */

  const liveRows = ref<DetectionRow[]>([])
  const liveFrameLabel = ref('')
  const liveFrameIndex = ref(0)
  const liveFrameCounts = ref<CountMap>({})
  const liveCumulativeObjects = ref(0)
  const liveUniqueObjects = ref(0)
  const liveUniqueByClass = ref<CountMap>({})
  const liveFrameTotal = ref(0)
  const liveActiveTracks = ref(0)
  const liveInferenceMs = ref<number | null>(null)
  const liveElapsedSeconds = ref(0)
  const liveSessionId = ref<number | null>(null)

  /* ------------------------------ 选中联动 ------------------------------ */

  const selectedRowKey = ref<string | null>(null)
  const hoveredRowKey = ref<string | null>(null)

  /* ------------------------------ 派生数据 ------------------------------ */

  const activeBatchItem = computed<BatchItemOut | null>(() => {
    const items = batchResult.value?.items
    if (!items || items.length === 0) return null
    return items[batchIndex.value] ?? null
  })

  const activeDetections = computed<Detection[]>(() => {
    if (mode.value === 'batch') return activeBatchItem.value?.detections ?? []
    if (mode.value === 'image') return imageResult.value?.detections ?? []
    return []
  })

  const activeCounts = computed<CountMap>(() => {
    if (mode.value === 'batch') return activeBatchItem.value?.counts ?? {}
    if (mode.value === 'image') return imageResult.value?.counts ?? {}
    if (mode.value === 'camera') return liveFrameCounts.value
    return {}
  })

  /**
   * **整个批次**的检测行。仅批量模式下与 `activeFrameRows` 不同：
   * 表格拿它当批次明细日志（每行自带 `frameLabel` 标明属于哪张图）。
   */
  const activeRows = computed<DetectionRow[]>(() => {
    if (mode.value === 'batch') return batchRows.value
    if (mode.value === 'image') return imageRows.value
    if (mode.value === 'camera') return liveRows.value
    return []
  })

  /**
   * **当前这一帧/这一张**的检测行 —— 画布、"当前画面记录"、"画面最高分"用它。
   *
   * 批量模式下底图（`previewUrl`）只有**当前那一张**，而 `activeRows` 是整个批次，
   * 两者混用会把全批次的框都画到当前图上（实测 57 张的框叠在一张 416×416 的图上，
   * 看起来就像满屏乱框）。
   *
   * 这里刻意**过滤** `batchRows` 而不是用 `toDetectionRows` 重建：后者的 key 是
   * 每次调用新生成的，重建出来的 key 对不上表格行，画布框与表格的选中/悬停联动会断。
   */
  const activeFrameRows = computed<DetectionRow[]>(() => {
    if (mode.value !== 'batch') return activeRows.value
    return batchRows.value.filter((row) => row.frameIndex === batchIndex.value)
  })

  /** 画面标识：文件名或帧号 —— 绝不能是 dataURL */
  const activeFrameLabel = computed<string>(() => {
    if (mode.value === 'batch') return activeBatchItem.value?.filename ?? '—'
    if (mode.value === 'image') {
      return imageResult.value
        ? localLabel.value || `任务 ${formatTaskNo(imageResult.value.task_id)}`
        : '—'
    }
    if (mode.value === 'camera') return liveFrameLabel.value || '实时画面'
    if (mode.value === 'video') return videoAccepted.value?.source_name ?? '—'
    return '—'
  })

  const currentTaskId = computed<number | null>(() => {
    if (mode.value === 'image') return imageResult.value?.task_id ?? null
    if (mode.value === 'batch') return batchResult.value?.task_id ?? null
    if (mode.value === 'video') return videoAccepted.value?.task_id ?? null
    return null
  })

  const annotatedUrl = computed<string>(() => {
    if (mode.value === 'image') return imageResult.value?.annotated_url ?? ''
    if (mode.value === 'batch') return activeBatchItem.value?.image_url ?? ''
    return ''
  })

  const previewUrl = computed<string>(() => {
    if (mode.value === 'image') return objectUrl.value ?? ''
    if (mode.value === 'batch') return activeBatchItem.value?.image_url ?? ''
    return ''
  })

  const frameTotal = computed<number>(() => {
    if (mode.value === 'image') return imageResult.value?.frame_total ?? 0
    if (mode.value === 'batch') return activeBatchItem.value?.frame_total ?? 0
    if (mode.value === 'camera') return liveFrameTotal.value
    return 0
  })

  const uniqueObjects = computed<number>(() => {
    if (mode.value === 'image') return imageResult.value?.unique_objects ?? 0
    if (mode.value === 'batch') return batchResult.value?.total_objects ?? 0
    if (mode.value === 'camera') return liveUniqueObjects.value
    if (mode.value === 'video') return videoTask.value?.unique_objects ?? 0
    return 0
  })

  const totalObjects = computed<number>(() => {
    if (mode.value === 'image') return imageResult.value?.frame_total ?? 0
    if (mode.value === 'batch') return batchResult.value?.total_objects ?? 0
    if (mode.value === 'camera') return liveCumulativeObjects.value
    if (mode.value === 'video') return videoTask.value?.total_objects ?? 0
    return 0
  })

  const inferenceMs = computed<number | null>(() => {
    if (mode.value === 'image') return imageResult.value?.inference_ms ?? null
    if (mode.value === 'batch') return activeBatchItem.value?.inference_ms ?? null
    if (mode.value === 'camera') return liveInferenceMs.value
    return null
  })

  const isRunning = computed(() => phase.value === 'uploading' || phase.value === 'running')

  const hasResult = computed(() => {
    if (mode.value === 'image') return imageResult.value !== null
    if (mode.value === 'batch') return batchResult.value !== null
    if (mode.value === 'video') return videoAccepted.value !== null
    if (mode.value === 'camera') return liveFrameIndex.value > 0
    return false
  })

  /* ------------------------------ 行为 ------------------------------ */

  function revokeObjectUrl(): void {
    if (objectUrl.value) {
      URL.revokeObjectURL(objectUrl.value)
      objectUrl.value = null
    }
  }

  function revokeVideoSource(): void {
    if (videoSourceObjectUrl.value) {
      URL.revokeObjectURL(videoSourceObjectUrl.value)
      videoSourceObjectUrl.value = null
    }
  }

  function setLocalPreview(file: File | null): void {
    revokeObjectUrl()
    if (file) {
      objectUrl.value = URL.createObjectURL(file)
      localLabel.value = file.name
    } else {
      localLabel.value = ''
    }
  }

  function setConf(value: number): void {
    conf.value = Math.min(0.99, Math.max(0, Number(value.toFixed(2))))
  }

  function setIou(value: number): void {
    iou.value = Math.min(0.99, Math.max(0, Number(value.toFixed(2))))
  }

  function setTargetFps(value: number): void {
    targetFps.value = Math.min(30, Math.max(1, Math.round(value)))
  }

  function setStride(value: number): void {
    stride.value = Math.min(20, Math.max(1, Math.round(value)))
  }

  function stopVideoPolling(): void {
    if (videoPollTimer.value !== null && typeof window !== 'undefined') {
      window.clearInterval(videoPollTimer.value)
    }
    videoPollTimer.value = null
  }

  function cancelCurrent(): void {
    abortController.value?.abort()
    abortController.value = null
    stopVideoPolling()
  }

  function resetResults(): void {
    phase.value = 'idle'
    errorMessage.value = null
    progress.value = 0
    progressLabel.value = ''
    imageResult.value = null
    imageRows.value = []
    batchResult.value = null
    batchRows.value = []
    batchIndex.value = 0
    videoTask.value = null
    selectedRowKey.value = null
    hoveredRowKey.value = null
    stopVideoPolling()
  }

  function resetLiveState(): void {
    liveRows.value = []
    liveFrameCounts.value = {}
    liveFrameLabel.value = ''
    liveFrameIndex.value = 0
    liveFrameTotal.value = 0
    liveCumulativeObjects.value = 0
    liveUniqueObjects.value = 0
    liveUniqueByClass.value = {}
    liveActiveTracks.value = 0
    liveInferenceMs.value = null
    liveElapsedSeconds.value = 0
    liveSessionId.value = null
  }

  function switchMode(next: DetectMode): void {
    if (mode.value === next) return
    cancelCurrent()
    mode.value = next
    errorMessage.value = null
    selectedRowKey.value = null
    hoveredRowKey.value = null
    if (next !== 'image') revokeObjectUrl()
    if (next !== 'camera') resetLiveState()
  }

  /* ---------------------------- 单图检测 ---------------------------- */

  async function runImage(file: File): Promise<ImageDetectionOut | null> {
    abortController.value?.abort()
    const controller = new AbortController()
    abortController.value = controller

    setLocalPreview(file)
    imageResult.value = null
    imageRows.value = []
    selectedRowKey.value = null
    errorMessage.value = null
    phase.value = 'uploading'
    progress.value = 0
    progressLabel.value = '正在上传图像'

    try {
      const result = await detectImage({
        file,
        conf: conf.value,
        iou: iou.value,
        signal: controller.signal,
        onProgress: (ratio) => {
          progress.value = ratio
          if (ratio >= 1) {
            progressLabel.value = '正在推理'
            phase.value = 'running'
          }
        }
      })
      imageResult.value = result
      imageRows.value = toDetectionRows(result.detections, file.name, 0, 'img')
      phase.value = 'done'
      progress.value = 1
      progressLabel.value = ''
      return result
    } catch (err) {
      if (controller.signal.aborted) {
        phase.value = 'idle'
        return null
      }
      const message = notifyApiError(err, false)
      errorMessage.value = message
      phase.value = 'error'
      progressLabel.value = ''
      return null
    }
  }

  /* ---------------------------- 批量检测 ---------------------------- */

  async function runBatch(files: File[]): Promise<BatchDetectionOut | null> {
    if (files.length === 0) return null
    let selected = files
    if (files.length > BATCH_MAX_FILES) {
      selected = files.slice(0, BATCH_MAX_FILES)
      notifyWarning(`后端单次最多处理 ${BATCH_MAX_FILES} 张，已截取前 ${BATCH_MAX_FILES} 张`)
    }

    abortController.value?.abort()
    const controller = new AbortController()
    abortController.value = controller

    batchResult.value = null
    batchRows.value = []
    batchIndex.value = 0
    selectedRowKey.value = null
    errorMessage.value = null
    phase.value = 'uploading'
    progress.value = 0
    progressLabel.value = `正在上传 ${selected.length} 张图像`

    try {
      const result = await detectBatch({
        files: selected,
        conf: conf.value,
        iou: iou.value,
        signal: controller.signal,
        onProgress: (ratio) => {
          progress.value = ratio
          if (ratio >= 1) {
            progressLabel.value = `正在批量推理 ${selected.length} 张`
            phase.value = 'running'
          }
        }
      })
      batchResult.value = result
      buildBatchRows()
      batchIndex.value = 0
      phase.value = 'done'
      progress.value = 1
      progressLabel.value = ''
      if (result.failed > 0) {
        notifyWarning(`批量完成：成功 ${result.succeeded} 张，失败 ${result.failed} 张`)
      } else {
        notifySuccess(`批量完成：${result.succeeded} 张图像，共 ${result.total_objects} 个目标`)
      }
      return result
    } catch (err) {
      if (controller.signal.aborted) {
        phase.value = 'idle'
        return null
      }
      const message = notifyApiError(err, false)
      errorMessage.value = message
      phase.value = 'error'
      progressLabel.value = ''
      return null
    }
  }

  function buildBatchRows(): void {
    const items = batchResult.value?.items ?? []
    const rows: DetectionRow[] = []
    items.forEach((item, index) => {
      rows.push(...toDetectionRows(item.detections, item.filename, index, `batch${index}`))
    })
    batchRows.value = rows
    selectedRowKey.value = null
  }

  function selectBatchIndex(index: number): void {
    const items = batchResult.value?.items ?? []
    if (index < 0 || index >= items.length) return
    batchIndex.value = index
    selectedRowKey.value = null
    hoveredRowKey.value = null
  }

  function stepBatch(delta: number): void {
    const total = batchResult.value?.items.length ?? 0
    if (total === 0) return
    selectBatchIndex(Math.min(total - 1, Math.max(0, batchIndex.value + delta)))
  }

  /* ---------------------------- 视频检测 ---------------------------- */

  async function runVideo(file: File): Promise<boolean> {
    abortController.value?.abort()
    const controller = new AbortController()
    abortController.value = controller

    stopVideoPolling()
    videoTask.value = null
    errorMessage.value = null
    phase.value = 'uploading'
    progress.value = 0
    progressLabel.value = '正在上传视频'

    revokeVideoSource()
    videoSourceObjectUrl.value = URL.createObjectURL(file)

    try {
      const accepted = await detectVideo({
        file,
        conf: conf.value,
        iou: iou.value,
        stride: stride.value,
        signal: controller.signal,
        onProgress: (ratio) => {
          progress.value = ratio
          if (ratio >= 1) {
            progressLabel.value = '等待后台任务启动'
            phase.value = 'running'
          }
        }
      })
      videoAccepted.value = {
        task_id: accepted.task_id,
        source_name: accepted.source_name,
        total_frames: accepted.total_frames
      }
      phase.value = 'running'
      progressLabel.value = '后台正在逐帧推理'
      startVideoPolling(accepted.task_id)
      return true
    } catch (err) {
      if (controller.signal.aborted) {
        phase.value = 'idle'
        return false
      }
      const message = notifyApiError(err, false)
      errorMessage.value = message
      phase.value = 'error'
      progressLabel.value = ''
      return false
    }
  }

  function startVideoPolling(taskId: number): void {
    stopVideoPolling()
    if (typeof window === 'undefined') return
    const tick = async () => {
      try {
        const detail = await getTask(taskId)
        videoTask.value = detail
        progress.value = Math.min(1, Math.max(0, (detail.progress ?? 0) / 100))
        progressLabel.value =
          detail.status === 'pending'
            ? '任务排队中'
            : `后台推理中 ${detail.frame_count ?? 0}/${detail.total_frames ?? 0} 帧`
        if (detail.status === 'done') {
          phase.value = 'done'
          progress.value = 1
          progressLabel.value = ''
          stopVideoPolling()
          notifySuccess(
            `视频检测完成：去重目标 ${detail.unique_objects ?? 0} 个，累计框 ${detail.total_objects ?? 0} 个`
          )
        } else if (detail.status === 'failed') {
          phase.value = 'error'
          errorMessage.value = detail.error ?? '视频任务失败'
          progressLabel.value = ''
          stopVideoPolling()
        }
      } catch (err) {
        errorMessage.value = notifyApiError(err)
        phase.value = 'error'
        progressLabel.value = ''
        stopVideoPolling()
      }
    }
    void tick()
    videoPollTimer.value = window.setInterval(() => {
      void tick()
    }, 1500)
  }

  /* ---------------------------- 摄像头帧结果 ---------------------------- */

  function applyLiveFrame(params: {
    detections: Detection[]
    seq: number
    frameTotal: number
    frameByClass: CountMap
    totalObjects: number
    uniqueObjects: number
    uniqueByClass: CountMap
    activeTracks: number
    inferenceMs: number
    elapsedS: number
  }): void {
    liveRows.value = toDetectionRows(params.detections, `帧 ${params.seq}`, params.seq, 'live')
    liveFrameCounts.value = params.frameByClass ?? {}
    liveFrameLabel.value = `帧 ${params.seq}`
    liveFrameIndex.value = params.seq
    liveFrameTotal.value = params.frameTotal
    liveCumulativeObjects.value = params.totalObjects
    liveUniqueObjects.value = params.uniqueObjects
    liveUniqueByClass.value = params.uniqueByClass ?? {}
    liveActiveTracks.value = params.activeTracks
    liveInferenceMs.value = params.inferenceMs
    liveElapsedSeconds.value = params.elapsedS
  }

  function setLiveSession(sessionId: number): void {
    liveSessionId.value = sessionId
  }

  function selectRow(key: string | null): void {
    selectedRowKey.value = key
    // 批量模式下表格是**整个批次**的明细，而画布只铺当前那一张图：点到别的图的
    // 行时必须跟着切图，否则"已选中 1 个目标"在画布上根本找不到对应的框。
    // 这里直接改 batchIndex —— 不能走 selectBatchIndex，它会把刚设的选中清掉。
    if (key && mode.value === 'batch') {
      const row = rowDetectionsOf(key)
      if (row && row.frameIndex !== batchIndex.value) batchIndex.value = row.frameIndex
    }
  }

  function hoverRow(key: string | null): void {
    hoveredRowKey.value = key
  }

  function rowDetectionsOf(key: string | null): DetectionRow | null {
    if (!key) return null
    return activeRows.value.find((row) => row.key === key) ?? null
  }

  function dispose(): void {
    cancelCurrent()
    revokeObjectUrl()
    revokeVideoSource()
    resetLiveState()
  }

  return {
    conf,
    iou,
    stride,
    targetFps,
    jpegQuality,
    setConf,
    setIou,
    setStride,
    setTargetFps,
    mode,
    phase,
    errorMessage,
    progress,
    progressLabel,
    switchMode,
    resetResults,
    cancelCurrent,
    isRunning,
    hasResult,
    currentTaskId,
    objectUrl,
    localLabel,
    imageResult,
    imageRows,
    setLocalPreview,
    runImage,
    batchResult,
    batchIndex,
    batchRows,
    activeBatchItem,
    runBatch,
    selectBatchIndex,
    stepBatch,
    videoAccepted,
    videoTask,
    videoSourceObjectUrl,
    runVideo,
    stopVideoPolling,
    liveRows,
    liveFrameLabel,
    liveFrameIndex,
    liveFrameCounts,
    liveCumulativeObjects,
    liveUniqueObjects,
    liveUniqueByClass,
    liveFrameTotal,
    liveActiveTracks,
    liveInferenceMs,
    liveElapsedSeconds,
    liveSessionId,
    applyLiveFrame,
    setLiveSession,
    resetLiveState,
    selectedRowKey,
    hoveredRowKey,
    selectRow,
    hoverRow,
    rowDetectionsOf,
    activeDetections,
    activeCounts,
    activeRows,
    activeFrameRows,
    activeFrameLabel,
    annotatedUrl,
    previewUrl,
    frameTotal,
    uniqueObjects,
    totalObjects,
    inferenceMs,
    dispose
  }
})
