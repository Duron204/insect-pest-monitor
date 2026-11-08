import { ElMessage, ElMessageBox } from 'element-plus'
import type { NormalizedError } from '@/api/client'

/**
 * 统一提示入口。
 * 约束：全局禁止使用 alert()，所有反馈走 ElMessage 或页面内错误横幅。
 */

export function notifySuccess(message: string): void {
  ElMessage({ type: 'success', message, duration: 2600, grouping: true })
}

export function notifyInfo(message: string): void {
  ElMessage({ type: 'info', message, duration: 2600, grouping: true })
}

export function notifyWarning(message: string): void {
  ElMessage({ type: 'warning', message, duration: 3600, grouping: true })
}

export function notifyError(message: string): void {
  ElMessage({
    type: 'error',
    message,
    duration: 4200,
    showClose: true,
    grouping: true
  })
}

/** 按归一化错误提示；401 已由 axios 拦截器统一处理，默认不重复弹窗 */
export function notifyApiError(error: unknown, skipUnauthorized = true): string {
  const candidate = error as Partial<NormalizedError> | null
  if (candidate && typeof candidate === 'object' && 'message' in candidate) {
    const message = String(candidate.message ?? '请求失败')
    if (skipUnauthorized && candidate.unauthorized === true) return message
    notifyError(message)
    return message
  }
  if (error instanceof Error) {
    notifyError(error.message)
    return error.message
  }
  const text = String(error)
  notifyError(text)
  return text
}

export interface ConfirmOptions {
  title: string
  message: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}

/** 二次确认。返回 true 表示确认执行；取消返回 false（不抛异常） */
export async function confirmAction(options: ConfirmOptions): Promise<boolean> {
  try {
    await ElMessageBox.confirm(options.message, options.title, {
      confirmButtonText: options.confirmText ?? '确认',
      cancelButtonText: options.cancelText ?? '取消',
      type: options.danger ? 'warning' : 'info',
      customClass: 'ip-confirm',
      draggable: true,
      closeOnClickModal: false,
      distinguishCancelAndClose: true
    })
    return true
  } catch {
    return false
  }
}
