import { computed, onBeforeUnmount, ref, shallowRef, watch, type Ref } from 'vue'
import type { DetectionRow } from '@/types'

export interface AnnotatedCanvasOptions {
  /** 画布容器（决定显示尺寸） */
  containerRef: Ref<HTMLElement | null>
  imageUrl: Ref<string>
  rows: Ref<DetectionRow[]>
  hoveredKey: Ref<string | null>
  selectedKey: Ref<string | null>
  onSelect?: (key: string | null) => void
  onHover?: (key: string | null) => void
  /** 视频等场景下由外部驱动绘制 */
  manualRender?: boolean
}

/** 分类配色：与农业监测台的苔绿/琥珀基调一致，同色系区分 */
const CLASS_PALETTE = [
  '#2f9e5f',
  '#4fc07e',
  '#e8a33d',
  '#d9773d',
  '#4a90d9',
  '#8e6fd0',
  '#c0556f',
  '#3fb0a5',
  '#9bb43a',
  '#b98c2f'
]

function paletteFor(classId: number): string {
  const index = Math.abs(classId) % CLASS_PALETTE.length
  return CLASS_PALETTE[index] ?? CLASS_PALETTE[0] ?? '#2f9e5f'
}

/**
 * 在图片上叠加检测框：
 * - 滚轮缩放（0.2 ~ 8 倍）、拖拽平移
 * - 悬停高亮、点击选中（与右栏 / 表格联动）
 * - 中文标签 + 置信度
 */
export function useAnnotatedCanvas(options: AnnotatedCanvasOptions) {
  const canvasRef = shallowRef<HTMLCanvasElement | null>(null)
  const image = shallowRef<HTMLImageElement | null>(null)
  const imageWidth = ref(0)
  const imageHeight = ref(0)
  const imageError = ref<string | null>(null)
  const loading = ref(false)

  const zoom = ref(1)
  const offsetX = ref(0)
  const offsetY = ref(0)
  const dragging = ref(false)
  const containerSize = ref({ width: 0, height: 0 })

  let baseCanvas: HTMLCanvasElement | null = null
  let renderPending = false
  let loadToken = 0
  let panStart: { x: number; y: number; ox: number; oy: number } | null = null
  let observer: ResizeObserver | null = null
  /**
   * 最近一次实际用的画法（底图+框，或只画框）。
   *
   * manualRender 下 requestRender() 不能直接放弃重绘：新图 onload、容器尺寸变化
   * 这类事件**只有这个合成器自己知道**，组件无从感知，漏掉就会出现"画布停在旧画面/
   * 旧尺寸上，而框已经是新的"。所以改成重放最近一次的画法。
   */
  let lastPaint: (() => void) | null = null

  /* ---------------------------- 显示几何 ---------------------------- */

  const displayRect = computed(() => {
    const { width, height } = containerSize.value
    if (imageWidth.value === 0 || imageHeight.value === 0 || width === 0 || height === 0) {
      return { x: 0, y: 0, width: 0, height: 0, scale: 1 }
    }
    const scale = Math.min(width / imageWidth.value, height / imageHeight.value)
    const drawWidth = imageWidth.value * scale
    const drawHeight = imageHeight.value * scale
    return {
      x: (width - drawWidth) / 2,
      y: (height - drawHeight) / 2,
      width: drawWidth,
      height: drawHeight,
      scale
    }
  })

  const fitScale = computed(() => displayRect.value.scale)
  const zoomPercent = computed(() => Math.round(zoom.value * 100))
  const canZoomIn = computed(() => zoom.value < 8)
  const canZoomOut = computed(() => zoom.value > 0.2)
  const isPannable = computed(() => zoom.value > 1.001 || offsetX.value !== 0 || offsetY.value !== 0)

  const hasImage = computed(() => image.value !== null && imageWidth.value > 0)

  /* ---------------------------- 坐标换算 ---------------------------- */

  function screenToImage(clientX: number, clientY: number): { x: number; y: number } | null {
    const canvas = canvasRef.value
    const rect = displayRect.value
    if (!canvas || rect.scale === 0 || !hasImage.value) return null
    const bounds = canvas.getBoundingClientRect()
    const localX = clientX - bounds.left
    const localY = clientY - bounds.top
    const imageX = (localX - rect.x - offsetX.value) / (rect.scale * zoom.value)
    const imageY = (localY - rect.y - offsetY.value) / (rect.scale * zoom.value)
    return { x: imageX, y: imageY }
  }

  function imageToScreen(x: number, y: number): { x: number; y: number } {
    const rect = displayRect.value
    return {
      x: rect.x + offsetX.value + x * rect.scale * zoom.value,
      y: rect.y + offsetY.value + y * rect.scale * zoom.value
    }
  }

  function hitTest(x: number, y: number): DetectionRow | null {
    const list = options.rows.value
    for (let index = list.length - 1; index >= 0; index -= 1) {
      const row = list[index]
      if (!row) continue
      const [x1, y1, x2, y2] = row.bbox
      const left = Math.min(x1, x2)
      const right = Math.max(x1, x2)
      const top = Math.min(y1, y2)
      const bottom = Math.max(y1, y2)
      if (x >= left && x <= right && y >= top && y <= bottom) return row
    }
    return null
  }

  /* ---------------------------- 绘制 ---------------------------- */

  function ensureCanvasSize(): { context: CanvasRenderingContext2D; dpr: number } | null {
    const canvas = canvasRef.value
    if (!canvas) return null
    const dpr = Math.min(2, window.devicePixelRatio || 1)
    const width = Math.max(1, Math.round(containerSize.value.width))
    const height = Math.max(1, Math.round(containerSize.value.height))
    const pixelWidth = Math.round(width * dpr)
    const pixelHeight = Math.round(height * dpr)
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth
      canvas.height = pixelHeight
    }
    canvas.style.width = `${width}px`
    canvas.style.height = `${height}px`
    const context = canvas.getContext('2d')
    if (!context) return null
    context.setTransform(dpr, 0, 0, dpr, 0, 0)
    return { context, dpr }
  }

  function drawBox(
    context: CanvasRenderingContext2D,
    row: DetectionRow,
    active: boolean,
    selected: boolean
  ): void {
    const rect = displayRect.value
    const scale = rect.scale * zoom.value
    if (scale === 0) return
    const [x1, y1, x2, y2] = row.bbox
    const left = rect.x + offsetX.value + Math.min(x1, x2) * scale
    const top = rect.y + offsetY.value + Math.min(y1, y2) * scale
    const width = Math.abs(x2 - x1) * scale
    const height = Math.abs(y2 - y1) * scale
    if (width < 1 || height < 1) return

    const color = paletteFor(row.classId)
    context.save()
    context.lineJoin = 'round'

    if (active || selected) {
      context.fillStyle = selected ? 'rgba(47, 158, 95, 0.22)' : 'rgba(232, 163, 61, 0.16)'
      context.fillRect(left, top, width, height)
    }

    context.lineWidth = selected ? 2.5 : active ? 2 : 1.5
    context.strokeStyle = selected ? '#e8a33d' : color
    context.strokeRect(left + 0.5, top + 0.5, Math.max(0, width - 1), Math.max(0, height - 1))

    // 角标：四角加重，像仪表而不是纯矩形
    const corner = Math.min(12, Math.max(6, Math.min(width, height) / 4))
    context.lineWidth = 3
    context.beginPath()
    context.moveTo(left, top + corner)
    context.lineTo(left, top)
    context.lineTo(left + corner, top)
    context.moveTo(left + width - corner, top)
    context.lineTo(left + width, top)
    context.lineTo(left + width, top + corner)
    context.moveTo(left + width, top + height - corner)
    context.lineTo(left + width, top + height)
    context.lineTo(left + width - corner, top + height)
    context.moveTo(left + corner, top + height)
    context.lineTo(left, top + height)
    context.lineTo(left, top + height - corner)
    context.stroke()

    // 标签
    const scoreText = row.score.toFixed(2)
    const trackText = row.trackId !== null ? ` #${row.trackId}` : ''
    const label = `${row.zhName || row.className} ${scoreText}${trackText}`
    context.font = '12px ui-monospace, Consolas, Menlo, monospace'
    const textWidth = context.measureText(label).width
    const paddingX = 6
    const labelHeight = 18
    const labelWidth = textWidth + paddingX * 2
    let labelTop = top - labelHeight - 2
    if (labelTop < 0) labelTop = Math.min(top + 2, rect.y + rect.height - labelHeight)
    const labelLeft = Math.min(Math.max(0, left), Math.max(0, rect.x + rect.width - labelWidth))

    context.fillStyle = selected ? '#e8a33d' : color
    context.fillRect(labelLeft, labelTop, labelWidth, labelHeight)
    context.fillStyle = '#0b120e'
    context.textBaseline = 'middle'
    context.textAlign = 'left'
    context.fillText(label, labelLeft + paddingX, labelTop + labelHeight / 2 + 0.5)
    context.restore()
  }

  function drawGuides(context: CanvasRenderingContext2D): void {
    if (!hasImage.value) return
    const { width, height } = containerSize.value
    context.save()
    context.strokeStyle = 'rgba(230, 239, 232, 0.10)'
    context.lineWidth = 1
    // 三分法弱参考线，只有在放大时才出现，避免喧宾夺主
    if (zoom.value > 1.5) {
      for (let index = 1; index < 3; index += 1) {
        const x = (width / 3) * index
        const y = (height / 3) * index
        context.beginPath()
        context.moveTo(x, 0)
        context.lineTo(x, height)
        context.stroke()
        context.beginPath()
        context.moveTo(0, y)
        context.lineTo(width, y)
        context.stroke()
      }
    }
    context.restore()
  }

  function render(): void {
    lastPaint = render
    const prepared = ensureCanvasSize()
    if (!prepared) return
    const { context } = prepared
    const { width, height } = containerSize.value
    context.clearRect(0, 0, width, height)

    const source = image.value
    const rect = displayRect.value
    if (source && imageWidth.value > 0 && imageHeight.value > 0) {
      if (!baseCanvas) baseCanvas = document.createElement('canvas')
      if (baseCanvas.width !== imageWidth.value || baseCanvas.height !== imageHeight.value) {
        baseCanvas.width = imageWidth.value
        baseCanvas.height = imageHeight.value
      }
      const baseContext = baseCanvas.getContext('2d')
      if (baseContext) {
        baseContext.clearRect(0, 0, imageWidth.value, imageHeight.value)
        baseContext.drawImage(source, 0, 0, imageWidth.value, imageHeight.value)
      }
      context.imageSmoothingEnabled = true
      context.imageSmoothingQuality = 'high'
      context.drawImage(
        baseCanvas,
        rect.x + offsetX.value,
        rect.y + offsetY.value,
        rect.width * zoom.value,
        rect.height * zoom.value
      )
      drawGuides(context)
    }

    const hovered = options.hoveredKey.value
    const selected = options.selectedKey.value
    for (const row of options.rows.value) {
      drawBox(context, row, row.key === hovered, row.key === selected)
    }
  }

  function requestRender(): void {
    if (options.manualRender) {
      // 重放最近一次的画法；首次绘制前还没记录过，交给调用方的显式 redraw()
      lastPaint?.()
      return
    }
    if (renderPending) return
    renderPending = true
    window.requestAnimationFrame(() => {
      renderPending = false
      render()
    })
  }

  /* ---------------------------- 缩放 / 平移 ---------------------------- */

  function clampOffsets(): void {
    const rect = displayRect.value
    const { width, height } = containerSize.value
    const scaledWidth = rect.width * zoom.value
    const scaledHeight = rect.height * zoom.value
    const maxX = Math.max(0, (scaledWidth - width) / 2)
    const maxY = Math.max(0, (scaledHeight - height) / 2)
    offsetX.value = Math.min(maxX, Math.max(-maxX, offsetX.value))
    offsetY.value = Math.min(maxY, Math.max(-maxY, offsetY.value))
  }

  function zoomAtFactor(factor: number, clientX?: number, clientY?: number): void {
    if (!hasImage.value) return
    const previousZoom = zoom.value
    const nextZoom = Math.min(8, Math.max(0.2, previousZoom * factor))
    if (Math.abs(nextZoom - previousZoom) < 0.0001) return

    const rect = displayRect.value
    if (clientX !== undefined && clientY !== undefined && canvasRef.value) {
      const bounds = canvasRef.value.getBoundingClientRect()
      const localX = clientX - bounds.left
      const localY = clientY - bounds.top
      const anchorX = (localX - rect.x - offsetX.value) / previousZoom
      const anchorY = (localY - rect.y - offsetY.value) / previousZoom
      zoom.value = nextZoom
      offsetX.value = localX - rect.x - anchorX * nextZoom
      offsetY.value = localY - rect.y - anchorY * nextZoom
    } else {
      zoom.value = nextZoom
    }
    clampOffsets()
    requestRender()
  }

  function zoomIn(): void {
    zoomAtFactor(1.25)
  }

  function zoomOut(): void {
    zoomAtFactor(1 / 1.25)
  }

  function fit(): void {
    zoom.value = 1
    offsetX.value = 0
    offsetY.value = 0
    requestRender()
  }

  function actualSize(): void {
    const base = fitScale.value
    if (base === 0) return
    zoom.value = Math.min(8, Math.max(0.2, 1 / base))
    offsetX.value = 0
    offsetY.value = 0
    clampOffsets()
    requestRender()
  }

  /* ---------------------------- 事件 ---------------------------- */

  function onWheel(event: WheelEvent): void {
    if (!hasImage.value) return
    event.preventDefault()
    const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12
    zoomAtFactor(factor, event.clientX, event.clientY)
  }

  function onPointerDown(event: PointerEvent): void {
    if (event.button !== 0) return
    dragging.value = true
    panStart = { x: event.clientX, y: event.clientY, ox: offsetX.value, oy: offsetY.value }
    const canvas = canvasRef.value
    canvas?.setPointerCapture(event.pointerId)
  }

  function onPointerMove(event: PointerEvent): void {
    if (dragging.value && panStart) {
      offsetX.value = panStart.ox + (event.clientX - panStart.x)
      offsetY.value = panStart.oy + (event.clientY - panStart.y)
      clampOffsets()
      requestRender()
      return
    }
    const point = screenToImage(event.clientX, event.clientY)
    if (!point) {
      if (options.hoveredKey.value !== null) options.onHover?.(null)
      return
    }
    const hit = hitTest(point.x, point.y)
    const nextKey = hit ? hit.key : null
    if (nextKey !== options.hoveredKey.value) options.onHover?.(nextKey)
  }

  function onPointerUp(event: PointerEvent): void {
    if (!dragging.value) return
    dragging.value = false
    const canvas = canvasRef.value
    canvas?.releasePointerCapture(event.pointerId)
    const moved =
      panStart !== null &&
      (Math.abs(event.clientX - panStart.x) > 3 || Math.abs(event.clientY - panStart.y) > 3)
    panStart = null
    if (moved) return
    const point = screenToImage(event.clientX, event.clientY)
    if (!point) {
      options.onSelect?.(null)
      return
    }
    const hit = hitTest(point.x, point.y)
    options.onSelect?.(hit ? hit.key : null)
  }

  function onPointerLeave(): void {
    dragging.value = false
    panStart = null
    options.onHover?.(null)
  }

  function onDoubleClick(): void {
    if (zoom.value > 1.001) fit()
    else zoomIn()
  }

  /* ---------------------------- 图片加载 ---------------------------- */

  /**
   * 加载底图。**加载完成后必须自己重绘一次**（下面三处都调 render() 而不是 requestRender()）。
   *
   * 为什么不能用 requestRender()：`DetectionCanvas` 传的是 `manualRender: true`，而
   * requestRender() 在那个开关下第一行就 return。偏偏 loadImage 是**异步**的 ——
   * 组件在 imageUrl 变化的 watcher 里调的 redraw() 发生在图片还没解码完的时候，
   * 画的还是**上一张**的 image.value；等新图 onload 时如果不再补一次重绘，
   * 画布就会一直停在旧底图上，而框（rows）早已经是新的了。
   *
   * 实测症状：批量模式点「下一张」后，画布显示第 N-1 张画面、却叠着第 N 张的框
   * （C00006 的茎秆蚜虫群配着 C00007 的框），看起来就是"框全画错位置了"。
   */
  async function loadImage(url: string): Promise<void> {
    loadToken += 1
    const token = loadToken
    if (!url) {
      image.value = null
      imageWidth.value = 0
      imageHeight.value = 0
      imageError.value = null
      render()
      return
    }
    loading.value = true
    imageError.value = null
    const element = new Image()
    element.crossOrigin = 'anonymous'
    try {
      await new Promise<void>((resolve, reject) => {
        element.onload = () => resolve()
        element.onerror = () => reject(new Error('图像加载失败'))
        element.src = url
      })
      if (token !== loadToken) return
      image.value = element
      imageWidth.value = element.naturalWidth || element.width
      imageHeight.value = element.naturalHeight || element.height
      fit()
    } catch (err) {
      if (token !== loadToken) return
      image.value = null
      imageWidth.value = 0
      imageHeight.value = 0
      imageError.value = err instanceof Error ? err.message : '图像加载失败'
    } finally {
      if (token === loadToken) {
        loading.value = false
        // 这里不能换成 requestRender()：manualRender 下它是空操作，见函数头注释
        render()
      }
    }
  }

  /** 摄像头场景：外部 canvas 绘制后把图像交给这里做基准（避免重复解码） */
  function setManualImage(source: HTMLVideoElement | HTMLCanvasElement): void {
    const width = source instanceof HTMLVideoElement ? source.videoWidth : source.width
    const height = source instanceof HTMLVideoElement ? source.videoHeight : source.height
    if (width === 0 || height === 0) return
    imageWidth.value = width
    imageHeight.value = height
  }

  /** 显式设置基准尺寸（摄像头抽帧后，坐标空间是压缩过的 JPEG 尺寸） */
  function setManualSize(width: number, height: number): void {
    if (width <= 0 || height <= 0) return
    imageWidth.value = width
    imageHeight.value = height
    if (zoom.value <= 1.001) {
      offsetX.value = 0
      offsetY.value = 0
    }
    requestRender()
  }

  /**
   * 仅绘制叠加层（不画底图）。
   * 摄像头场景下 <video> 自身作为底图，canvas 只负责画框。
   */
  function drawOverlayOnly(): void {
    lastPaint = drawOverlayOnly
    const prepared = ensureCanvasSize()
    if (!prepared) return
    const { context } = prepared
    const { width, height } = containerSize.value
    context.clearRect(0, 0, width, height)
    if (!hasImage.value) return
    drawGuides(context)
    const hovered = options.hoveredKey.value
    const selected = options.selectedKey.value
    for (const row of options.rows.value) {
      drawBox(context, row, row.key === hovered, row.key === selected)
    }
  }

  /* ---------------------------- 生命周期 ---------------------------- */

  function observeContainer(): void {
    const element = options.containerRef.value
    if (!element || typeof ResizeObserver === 'undefined') return
    observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const box = entry.contentRect
      containerSize.value = { width: Math.round(box.width), height: Math.round(box.height) }
      if (zoom.value <= 1.001) {
        offsetX.value = 0
        offsetY.value = 0
      } else {
        clampOffsets()
      }
      requestRender()
    })
    observer.observe(element)
    const box = element.getBoundingClientRect()
    containerSize.value = { width: Math.round(box.width), height: Math.round(box.height) }
  }

  function disconnect(): void {
    observer?.disconnect()
    observer = null
  }

  watch(options.imageUrl, (url) => {
    void loadImage(url)
  })

  watch([options.rows, options.hoveredKey, options.selectedKey], () => requestRender(), {
    deep: false
  })

  onBeforeUnmount(disconnect)

  return {
    canvasRef,
    zoom,
    zoomPercent,
    canZoomIn,
    canZoomOut,
    isPannable,
    hasImage,
    loading,
    imageError,
    imageWidth,
    imageHeight,
    fit,
    zoomIn,
    zoomOut,
    actualSize,
    observeContainer,
    disconnect,
    loadImage,
    render,
    drawOverlayOnly,
    requestRender,
    setManualImage,
    setManualSize,
    onWheel,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerLeave,
    onDoubleClick,
    screenToImage,
    imageToScreen
  }
}
