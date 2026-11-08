import { del, download, get, saveBlob, type DownloadedFile } from './client'
import type {
  Detection,
  PageOut,
  TaskCountsOut,
  TaskDetailOut,
  TaskOut
} from '@/types'

/* ------------------------------- 历史 ------------------------------- */

export interface TaskQuery {
  page?: number
  page_size?: number
  mode?: string
  status?: string
  plot_id?: number
  since?: string
  until?: string
}

export function listTasks(query: TaskQuery = {}): Promise<PageOut<TaskOut>> {
  const params: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params[key] = value
  }
  return get<PageOut<TaskOut>>('/tasks', params)
}

export function getTask(id: number): Promise<TaskDetailOut> {
  return get<TaskDetailOut>(`/tasks/${id}`)
}

export interface DetectionQuery {
  page?: number
  page_size?: number
  frame_index?: number
  class_name?: string
}

export function listTaskDetections(
  id: number,
  query: DetectionQuery = {}
): Promise<PageOut<Detection>> {
  const params: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params[key] = value
  }
  return get<PageOut<Detection>>(`/tasks/${id}/detections`, params)
}

export function deleteTask(id: number): Promise<void> {
  return del<void>(`/tasks/${id}`)
}

export function getTaskCounts(id: number): Promise<TaskCountsOut> {
  return get<TaskCountsOut>(`/tasks/${id}/counts`)
}

export const REPORT_FORMATS = ['xlsx', 'docx', 'html'] as const
export type ReportFormat = (typeof REPORT_FORMATS)[number]

const REPORT_MIME: Record<ReportFormat, string> = {
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  html: 'text/html'
}

export function reportFilename(id: number, format: ReportFormat): string {
  return `task-${String(id).padStart(6, '0')}.${format}`
}

export function fetchReport(id: number, format: ReportFormat): Promise<DownloadedFile> {
  return download(`/tasks/${id}/report`, { format }, reportFilename(id, format))
}

export async function downloadReport(id: number, format: ReportFormat): Promise<string> {
  const file = await fetchReport(id, format)
  saveBlob(file.blob, file.filename || reportFilename(id, format))
  return file.filename
}

export interface PreviewUrlResult {
  url: string
  revoke: () => void
}

/** 报告预览：html 直接在浏览器新窗口打开，xlsx/docx 交给下载 */
export async function previewReport(id: number, format: ReportFormat): Promise<PreviewUrlResult | null> {
  if (format !== 'html') return null
  const file = await fetchReport(id, format)
  const typed = file.blob.type ? file.blob : new Blob([file.blob], { type: REPORT_MIME.html })
  const url = URL.createObjectURL(typed)
  return { url, revoke: () => URL.revokeObjectURL(url) }
}
