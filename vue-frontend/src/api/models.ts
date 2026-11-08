import { get, post, upload, type UploadOptions } from './client'
import type { ModelOut, ModelRegistryOut } from '@/types'

/* ---------------------------- 模型注册表 ---------------------------- */

export function listModels(): Promise<ModelRegistryOut> {
  return get<ModelRegistryOut>('/models')
}

export function selectModel(id: number): Promise<ModelOut> {
  return post<ModelOut>('/models/select', { id })
}

/** 仅支持 .pt 权重文件 */
export function uploadModel(file: File, options: UploadOptions = {}): Promise<ModelOut> {
  const form = new FormData()
  form.append('file', file, file.name)
  return upload<ModelOut>('/models/upload', form, options)
}

/** 从路径里取文件名，用于展示与下载命名 */
export function baseName(path: string | null | undefined): string {
  if (!path) return ''
  const normalized = path.replace(/\\/g, '/')
  const parts = normalized.split('/')
  return parts[parts.length - 1] ?? path
}
