/**
 * 画布底图必须跟着 `imageUrl` 一起换。
 *
 * 回归的 bug：`DetectionCanvas` 传的是 `manualRender: true`，而合成器里的
 * `requestRender()` 在那个开关下第一行就 return。偏偏 `loadImage()` 是**异步**的 ——
 * 组件在 imageUrl 的 watcher 里同步调 `redraw()` 时新图还没解码完，画的还是
 * 上一张的 `image.value`；等新图 onload 之后再没人补一次重绘。
 *
 * 现场症状：批量模式点「下一张」后，画布是第 N-1 张画面，框却是第 N 张的框
 * （实测 C00006 的茎秆蚜虫群配着 C00007 的框），看着就像"框全画到错位置了"。
 *
 * 这里不挂 `DetectionCanvas`（它用了 Element Plus，按项目约定不在单测里注册），
 * 直接测合成器，并**照抄组件那两步调用顺序**：先掉 loadImage，紧接着同步 render()。
 */
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useAnnotatedCanvas } from './useAnnotatedCanvas'
import type { DetectionRow } from '@/types'

const SIZE = 416
/** 所有被画上去的图源，按调用顺序（可见画布与内部 baseCanvas 混在一起） */
const paintedSources: unknown[] = []
const imageRegistry = new Map<string, FakeImage>()

class FakeImage {
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  crossOrigin = ''
  naturalWidth = 0
  naturalHeight = 0
  width = 0
  height = 0
  #src = ''

  set src(value: string) {
    this.#src = value
    // 真实浏览器里 onload 一定在后续任务里触发，这里用微任务模拟
    queueMicrotask(() => {
      this.naturalWidth = SIZE
      this.naturalHeight = SIZE
      this.width = SIZE
      this.height = SIZE
      imageRegistry.set(value, this)
      this.onload?.()
    })
  }

  get src(): string {
    return this.#src
  }
}

type ResizeCallback = (entries: { contentRect: { width: number; height: number } }[]) => void

let resizeCallback: ResizeCallback | null = null

/** 把容器尺寸变化手动喂给 ResizeObserver 的回调（jsdom 没有布局，不会自己触发） */
function fireResize(width: number, height: number): void {
  resizeCallback?.([{ contentRect: { width, height } }])
}

/** 只挑底图那一类 `drawImage(source, ...)`：源是 HTMLImageElement，不是内部 canvas */
function paintedImageUrls(): string[] {
  return paintedSources.filter((source): source is FakeImage => source instanceof FakeImage).map((el) => el.src)
}

beforeEach(() => {
  paintedSources.length = 0
  imageRegistry.clear()
  vi.stubGlobal('Image', FakeImage)
  resizeCallback = null
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: ResizeCallback) {
        resizeCallback = callback
      }
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
  )
  // jsdom 不实现 canvas 2D；返回一个只记录 drawImage 的空壳就够了
  HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement) {
    const proxy = new Proxy(
      {},
      {
        get(_target, property) {
          if (property === 'drawImage') {
            return (source: unknown) => {
              paintedSources.push(source)
            }
          }
          if (property === 'measureText') {
            return (text: string) => ({ width: String(text).length * 7 })
          }
          return () => undefined
        },
        set() {
          return true
        }
      }
    )
    return proxy as unknown as CanvasRenderingContext2D
  } as unknown as HTMLCanvasElement['getContext']
})

function createHarness() {
  const container = document.createElement('div')
  const canvasEl = document.createElement('canvas')
  document.body.append(container)

  const canvas = useAnnotatedCanvas({
    containerRef: ref(container),
    imageUrl: ref('a.jpg'),
    rows: ref<DetectionRow[]>([]),
    hoveredKey: ref<string | null>(null),
    selectedKey: ref<string | null>(null),
    manualRender: true
  })
  canvas.canvasRef.value = canvasEl
  canvas.observeContainer()
  fireResize(800, 600)
  return canvas
}

describe('useAnnotatedCanvas 的底图刷新', () => {
  it('换图之后，新底图必须真的被画上去（异步加载完成时要补一次重绘）', async () => {
    const canvas = createHarness()

    // 组件的调用顺序：先发起加载，紧接着同步重绘（此刻新图还没解码完）
    const first = canvas.loadImage('a.jpg')
    canvas.render()
    await first

    const second = canvas.loadImage('b.jpg')
    canvas.render()
    await second

    const painted = paintedImageUrls()
    expect(painted).toContain('b.jpg')
    expect(painted.lastIndexOf('b.jpg')).toBeGreaterThan(painted.indexOf('a.jpg'))
  })

  it('容器尺寸变化后要重绘（manualRender 下 requestRender 不能是空操作）', async () => {
    const canvas = createHarness()
    const pending = canvas.loadImage('a.jpg')
    canvas.render()
    await pending

    const before = paintedImageUrls().length
    fireResize(1000, 700)

    expect(paintedImageUrls().length).toBeGreaterThan(before)
  })
})
