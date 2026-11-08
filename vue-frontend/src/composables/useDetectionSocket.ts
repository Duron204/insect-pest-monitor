import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'
import { ElMessage } from 'element-plus'
import { detectionSocketUrl } from '@/api/detection'
import { getToken } from '@/api/client'
import { errorText } from '@/utils'
import type {
  CountMap,
  Detection,
  DetectionSocketState,
  WsErrorMessage,
  WsFrameResultMessage,
  WsInboundMessage,
  WsParams,
  WsReadyMessage,
  WsTaskFinishedMessage
} from '@/types'

export interface FrameResultPayload {
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
}

export interface DetectionSocketOptions {
  onReady?: (message: WsReadyMessage) => void
  onFrameResult?: (payload: FrameResultPayload) => void
  onTaskStarted?: (sessionId: number) => void
  onTaskFinished?: (message: WsTaskFinishedMessage) => void
  onParamsUpdated?: (params: WsParams) => void
  onModelChanged?: (detector: Record<string, unknown>) => void
  onError?: (message: WsErrorMessage) => void
  onStateChange?: (state: DetectionSocketState) => void
}

const BACKOFF_BASE_MS = 1000
const BACKOFF_MAX_MS = 15000
const HEARTBEAT_MS = 20000
const MAX_RECONNECT_ATTEMPTS = 6

function payloadText(payload: unknown): string | null {
  if (typeof payload === 'string') return payload
  if (payload && typeof payload === 'object' && 'data' in payload) {
    const data = (payload as { data?: unknown }).data
    if (typeof data === 'string') return data
  }
  return null
}

/**
 * 摄像头实时检测的 WebSocket 通道。
 * - 同源推导地址，不硬编码端口
 * - 连上后第一条消息固定为 {"type":"auth","token":...}
 * - 断线指数退避重连 1s → 2s → 4s … 封顶 15s，并维持心跳
 */
export function useDetectionSocket(options: DetectionSocketOptions = {}) {
  const socket = shallowRef<WebSocket | null>(null)
  const state = ref<DetectionSocketState>('idle')
  const retryCount = ref(0)
  const lastError = ref<string | null>(null)
  const nextRetryInMs = ref(0)
  const readyMessage = ref<WsReadyMessage | null>(null)
  const busy = ref(false)

  let heartbeatTimer: number | null = null
  let reconnectTimer: number | null = null
  let countdownTimer: number | null = null
  let manuallyClosed = false

  const isReady = computed(() => state.value === 'ready')
  const isActive = computed(
    () =>
      state.value === 'open' ||
      state.value === 'ready' ||
      state.value === 'connecting' ||
      state.value === 'reconnecting'
  )

  const stateLabel = computed(() => {
    switch (state.value) {
      case 'idle':
        return '未连接'
      case 'connecting':
        return '连接中'
      case 'open':
        return '握手中'
      case 'ready':
        return '已连接'
      case 'reconnecting':
        return nextRetryInMs.value > 0 ? `重连中 ${Math.ceil(nextRetryInMs.value / 1000)}s` : '重连中'
      case 'closed':
        return '已断开'
      case 'error':
        return '连接异常'
      default:
        return '未知'
    }
  })

  const stateTone = computed<'ok' | 'warn' | 'err' | 'off'>(() => {
    switch (state.value) {
      case 'ready':
        return 'ok'
      case 'connecting':
      case 'open':
      case 'reconnecting':
        return 'warn'
      case 'error':
        return 'err'
      default:
        return 'off'
    }
  })

  function setState(next: DetectionSocketState): void {
    state.value = next
    options.onStateChange?.(next)
  }

  function clearTimers(): void {
    if (heartbeatTimer !== null) {
      window.clearInterval(heartbeatTimer)
      heartbeatTimer = null
    }
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (countdownTimer !== null) {
      window.clearInterval(countdownTimer)
      countdownTimer = null
    }
    nextRetryInMs.value = 0
  }

  function rawSend(payload: Record<string, unknown>): boolean {
    const current = socket.value
    if (!current || current.readyState !== WebSocket.OPEN) return false
    try {
      current.send(JSON.stringify(payload))
      return true
    } catch (err) {
      lastError.value = errorText(err)
      return false
    }
  }

  function startHeartbeat(): void {
    if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer)
    heartbeatTimer = window.setInterval(() => {
      if (!rawSend({ type: 'ping' })) {
        // 心跳发不出去说明链路已经坏了，主动关闭以触发重连
        const current = socket.value
        if (current && current.readyState === WebSocket.OPEN) {
          current.close(4000, 'heartbeat failed')
        }
      }
    }, HEARTBEAT_MS)
  }

  function scheduleReconnect(): void {
    if (manuallyClosed) return
    if (retryCount.value >= MAX_RECONNECT_ATTEMPTS) {
      setState('closed')
      lastError.value = `重连 ${MAX_RECONNECT_ATTEMPTS} 次后仍未成功，请检查后端服务`
      return
    }
    const delay = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** retryCount.value)
    retryCount.value += 1
    setState('reconnecting')
    nextRetryInMs.value = delay
    if (countdownTimer !== null) window.clearInterval(countdownTimer)
    countdownTimer = window.setInterval(() => {
      nextRetryInMs.value = Math.max(0, nextRetryInMs.value - 1000)
      if (nextRetryInMs.value === 0 && countdownTimer !== null) {
        window.clearInterval(countdownTimer)
        countdownTimer = null
      }
    }, 1000)
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      nextRetryInMs.value = 0
      open()
    }, delay)
  }

  function handleMessage(payload: unknown): void {
    const text = payloadText(payload)
    if (text === null) return
    let message: WsInboundMessage | null = null
    try {
      message = JSON.parse(text) as WsInboundMessage
    } catch {
      return
    }
    if (!message || typeof message.type !== 'string') return

    switch (message.type) {
      case 'ready': {
        readyMessage.value = message
        busy.value = false
        retryCount.value = 0
        lastError.value = null
        setState('ready')
        startHeartbeat()
        options.onReady?.(message)
        break
      }
      case 'task_started': {
        options.onTaskStarted?.(message.session_id)
        break
      }
      case 'frame_result': {
        const result = message as WsFrameResultMessage
        options.onFrameResult?.({
          detections: result.detections ?? [],
          seq: result.seq ?? 0,
          frameTotal: result.frame_total ?? 0,
          frameByClass: result.frame_by_class ?? {},
          totalObjects: result.total_objects ?? 0,
          uniqueObjects: result.unique_objects ?? 0,
          uniqueByClass: result.unique_by_class ?? {},
          activeTracks: result.active_tracks ?? 0,
          inferenceMs: result.inference_ms ?? 0,
          elapsedS: result.elapsed_s ?? 0
        })
        break
      }
      case 'params_updated': {
        options.onParamsUpdated?.(message.params)
        break
      }
      case 'model_changed': {
        options.onModelChanged?.(message.detector ?? {})
        break
      }
      case 'task_finished': {
        options.onTaskFinished?.(message)
        break
      }
      case 'pong':
        break
      case 'error': {
        const errorMessage = message
        lastError.value = errorMessage.message || errorMessage.code
        if (errorMessage.code === 'busy') {
          busy.value = true
          manuallyClosed = true
          ElMessage({
            type: 'warning',
            message: '后端实时通道正被占用（单实例设计：同时只允许 1 路流），请稍后重试',
            duration: 5000,
            showClose: true
          })
        } else if (errorMessage.code === 'unauthorized' || errorMessage.code === 'auth_required') {
          manuallyClosed = true
          ElMessage({
            type: 'error',
            message: '实时检测鉴权失败，请重新登录',
            duration: 4200,
            showClose: true
          })
        } else if (errorMessage.code === 'model_not_loaded') {
          ElMessage({
            type: 'warning',
            message: '后端模型尚未加载，请先在「模型管理」中选择一个模型',
            duration: 4200,
            showClose: true
          })
        } else if (errorMessage.code === 'bad_frame') {
          ElMessage({ type: 'warning', message: '有一帧图像编码异常，已跳过', duration: 2600 })
        }
        options.onError?.(errorMessage)
        break
      }
      default:
        break
    }
  }

  function open(): void {
    if (typeof WebSocket === 'undefined') return
    manuallyClosed = false
    clearTimers()

    const token = getToken()
    if (!token) {
      setState('error')
      lastError.value = '缺少登录凭证，无法建立实时通道'
      return
    }

    setState(retryCount.value > 0 ? 'reconnecting' : 'connecting')

    let ws: WebSocket
    try {
      ws = new WebSocket(detectionSocketUrl())
    } catch (err) {
      lastError.value = errorText(err)
      scheduleReconnect()
      return
    }

    socket.value = ws

    ws.onopen = () => {
      setState('open')
      // 第一条消息必须是 auth，否则服务端会直接关闭
      rawSend({ type: 'auth', token })
    }

    ws.onmessage = (event: MessageEvent) => {
      handleMessage(event)
    }

    ws.onerror = () => {
      lastError.value = '实时通道发生错误'
    }

    ws.onclose = (event: CloseEvent) => {
      if (heartbeatTimer !== null) {
        window.clearInterval(heartbeatTimer)
        heartbeatTimer = null
      }
      socket.value = null
      if (manuallyClosed) {
        setState('closed')
        return
      }
      if (event.code === 4401 || event.code === 4403) {
        manuallyClosed = true
        setState('error')
        lastError.value = '实时通道鉴权失败（401/403）'
        return
      }
      scheduleReconnect()
    }
  }

  function close(userInitiated = true): void {
    manuallyClosed = true
    clearTimers()
    const current = socket.value
    if (current) {
      if (current.readyState === WebSocket.OPEN) {
        rawSend({ type: 'stop' })
      }
      current.close(1000, userInitiated ? 'client closing' : 'unmount')
    }
    socket.value = null
    setState('closed')
  }

  function sendFrame(base64Jpeg: string, seq: number): boolean {
    return rawSend({ type: 'frame', image: base64Jpeg, seq })
  }

  function updateParams(conf: number, iou: number): boolean {
    return rawSend({ type: 'update_params', conf, iou })
  }

  function selectModel(id: number): boolean {
    return rawSend({ type: 'select_model', id })
  }

  function stopStream(): boolean {
    return rawSend({ type: 'stop' })
  }

  onBeforeUnmount(() => {
    close(false)
  })

  return {
    state,
    stateLabel,
    stateTone,
    isReady,
    isActive,
    readyMessage,
    retryCount,
    lastError,
    nextRetryInMs,
    busy,
    open,
    close,
    sendFrame,
    updateParams,
    selectModel,
    stopStream
  }
}
