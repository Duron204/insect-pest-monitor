import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'

export interface CameraCaptureOptions {
  /** 抽帧频率，默认 10fps */
  fps?: number
  /** JPEG 质量，默认 0.7 */
  quality?: number
  /** 最长边像素上限，避免单帧过大 */
  maxEdge?: number
  /** 每帧回调：base64 JPEG（不含 data: 前缀） */
  onFrame?: (base64: string, seq: number) => void
  onError?: (message: string) => void
  onStarted?: (settings: MediaTrackSettings) => void
  onStopped?: () => void
}

/**
 * 摄像头抽帧：getUserMedia + canvas → base64 JPEG（不含 data: 前缀）
 */
export function useCameraCapture(options: CameraCaptureOptions = {}) {
  const videoRef = shallowRef<HTMLVideoElement | null>(null)
  const stream = shallowRef<MediaStream | null>(null)
  const active = ref(false)
  const starting = ref(false)
  const error = ref<string | null>(null)
  const actualFps = ref(0)
  const frameSeq = ref(0)
  const frameSize = ref<{ width: number; height: number } | null>(null)

  const fps = ref(options.fps ?? 10)
  const quality = ref(options.quality ?? 0.7)
  const maxEdge = options.maxEdge ?? 960

  const trackSettings = ref<MediaTrackSettings | null>(null)

  let timer: number | null = null
  let captureCanvas: HTMLCanvasElement | null = null
  let lastTickAt = 0
  const measuredFps = ref(0)

  const supported = computed(() => {
    if (typeof navigator === 'undefined') return false
    const devices = navigator.mediaDevices as MediaDevices | undefined
    return typeof devices?.getUserMedia === 'function'
  })

  function stopTimer(): void {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
    actualFps.value = 0
  }

  function describeError(err: unknown): string {
    if (err && typeof err === 'object' && 'name' in err) {
      const name = String((err as { name?: unknown }).name ?? '')
      const message = String((err as { message?: unknown }).message ?? '')
      switch (name) {
        case 'NotAllowedError':
          return '浏览器拒绝了摄像头权限，请在地址栏允许后重试'
        case 'NotFoundError':
        case 'DevicesNotFoundError':
          return '未检测到可用摄像头设备'
        case 'NotReadableError':
          return '摄像头被其他程序占用，请关闭占用后重试'
        case 'OverconstrainedError':
          return '摄像头不支持所请求的分辨率'
        case 'SecurityError':
          return '当前页面不是安全上下文（需 https 或 localhost）'
        default:
          return message || `摄像头启动失败：${name || '未知错误'}`
      }
    }
    if (err instanceof Error) return err.message
    return String(err)
  }

  async function start(): Promise<boolean> {
    if (active.value) return true
    starting.value = true
    error.value = null
    try {
      if (!supported.value) {
        throw new Error('当前浏览器不支持 getUserMedia（需要 https 或 localhost）')
      }
      const media = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'environment' },
        audio: false
      })
      stream.value = media
      const element = videoRef.value
      if (element) {
        element.srcObject = media
        element.muted = true
        element.playsInline = true
        await element.play().catch(() => undefined)
      }
      const track = media.getVideoTracks()[0]
      trackSettings.value = track ? track.getSettings() : null
      const settings = trackSettings.value
      if (settings && (settings.width ?? 0) > 0) {
        frameSize.value = { width: settings.width ?? 0, height: settings.height ?? 0 }
      }
      active.value = true
      options.onStarted?.(trackSettings.value ?? {})
      startTimer()
      return true
    } catch (err) {
      error.value = describeError(err)
      options.onError?.(error.value)
      await stop()
      return false
    } finally {
      starting.value = false
    }
  }

  /** 抓取当前 video 帧，返回 base64（不含 data: 前缀） */
  function grabFrame(): string | null {
    const element = videoRef.value
    if (!element) return null
    const sourceWidth = element.videoWidth
    const sourceHeight = element.videoHeight
    if (sourceWidth === 0 || sourceHeight === 0) return null

    const longest = Math.max(sourceWidth, sourceHeight)
    const scale = longest > maxEdge ? maxEdge / longest : 1
    const width = Math.max(1, Math.round(sourceWidth * scale))
    const height = Math.max(1, Math.round(sourceHeight * scale))

    if (!captureCanvas) captureCanvas = document.createElement('canvas')
    const canvas = captureCanvas
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext('2d')
    if (!context) return null
    context.drawImage(element, 0, 0, width, height)

    const dataUrl = canvas.toDataURL('image/jpeg', quality.value)
    const comma = dataUrl.indexOf(',')
    const base64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl
    frameSize.value = { width, height }
    return base64
  }

  function tick(): void {
    const base64 = grabFrame()
    if (!base64) return
    frameSeq.value += 1
    const now = performance.now()
    if (lastTickAt > 0) {
      const delta = now - lastTickAt
      if (delta > 0) {
        const instant = 1000 / delta
        measuredFps.value = measuredFps.value === 0 ? instant : measuredFps.value * 0.7 + instant * 0.3
        actualFps.value = Math.round(measuredFps.value * 10) / 10
      }
    }
    lastTickAt = now
    options.onFrame?.(base64, frameSeq.value)
  }

  function startTimer(): void {
    stopTimer()
    if (typeof window === 'undefined') return
    const interval = Math.max(33, Math.round(1000 / Math.max(1, fps.value)))
    lastTickAt = 0
    measuredFps.value = 0
    timer = window.setInterval(tick, interval)
  }

  function setFps(value: number): void {
    fps.value = Math.min(30, Math.max(1, Math.round(value)))
    if (active.value) startTimer()
  }

  function setQuality(value: number): void {
    quality.value = Math.min(0.95, Math.max(0.3, value))
  }

  async function stop(): Promise<void> {
    stopTimer()
    const media = stream.value
    if (media) {
      for (const track of media.getTracks()) track.stop()
    }
    stream.value = null
    const element = videoRef.value
    if (element) element.srcObject = null
    if (active.value) options.onStopped?.()
    active.value = false
    frameSeq.value = 0
  }

  onBeforeUnmount(() => {
    void stop()
  })

  return {
    videoRef,
    stream,
    active,
    starting,
    error,
    fps,
    actualFps,
    quality,
    frameSeq,
    frameSize,
    trackSettings,
    supported,
    start,
    stop,
    grabFrame,
    setFps,
    setQuality
  }
}
