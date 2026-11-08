import { upload } from './client'
import type { BatchDetectionOut, ImageDetectionOut, VideoTaskAcceptedOut } from '@/types'

/* ------------------------------- 检测 ------------------------------- */

export interface ImageDetectParams {
  file: File
  conf?: number
  iou?: number
  onProgress?: (ratio: number) => void
  signal?: AbortSignal
}

function appendParams(form: FormData, conf?: number, iou?: number): void {
  if (typeof conf === 'number') form.append('conf', String(conf))
  if (typeof iou === 'number') form.append('iou', String(iou))
}

export function detectImage(params: ImageDetectParams): Promise<ImageDetectionOut> {
  const form = new FormData()
  form.append('file', params.file, params.file.name)
  appendParams(form, params.conf, params.iou)
  return upload<ImageDetectionOut>('/detection/image', form, {
    onProgress: params.onProgress,
    signal: params.signal
  })
}

export interface BatchDetectParams {
  files: File[]
  conf?: number
  iou?: number
  onProgress?: (ratio: number) => void
  signal?: AbortSignal
}

/** 后端限制 ≤50 个文件 */
export const BATCH_MAX_FILES = 50

export function detectBatch(params: BatchDetectParams): Promise<BatchDetectionOut> {
  const form = new FormData()
  for (const file of params.files) {
    form.append('files', file, file.name)
  }
  appendParams(form, params.conf, params.iou)
  return upload<BatchDetectionOut>('/detection/batch', form, {
    onProgress: params.onProgress,
    signal: params.signal
  })
}

export interface VideoDetectParams {
  file: File
  conf?: number
  iou?: number
  stride?: number
  onProgress?: (ratio: number) => void
  signal?: AbortSignal
}

/** 202 Accepted，后台任务；随后轮询 GET /api/tasks/{id} */
export function detectVideo(params: VideoDetectParams): Promise<VideoTaskAcceptedOut> {
  const form = new FormData()
  form.append('file', params.file, params.file.name)
  appendParams(form, params.conf, params.iou)
  if (typeof params.stride === 'number') form.append('stride', String(params.stride))
  return upload<VideoTaskAcceptedOut>('/detection/video', form, {
    onProgress: params.onProgress,
    signal: params.signal
  })
}

/**
 * 摄像头实时检测的 WebSocket 地址：同源推导，绝不硬编码端口。
 */
export function detectionSocketUrl(): string {
  if (typeof window === 'undefined') return '/ws/detection'
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}/ws/detection`
}
