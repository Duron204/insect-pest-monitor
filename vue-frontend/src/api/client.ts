import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios'
import type { ApiErrorShape } from '@/types'

export const TOKEN_KEY = 'ip_token'
export const USER_KEY = 'ip_user'

/* ---------------------------- token 存取 ---------------------------- */

let memoryToken: string | null = null

function safeGetItem(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function safeSetItem(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    /* 隐私模式下 localStorage 可能不可用，降级到内存 */
  }
}

function safeRemoveItem(key: string): void {
  try {
    window.localStorage.removeItem(key)
  } catch {
    /* ignore */
  }
}

export function getToken(): string | null {
  if (memoryToken) return memoryToken
  const stored = safeGetItem(TOKEN_KEY)
  if (stored) memoryToken = stored
  return stored
}

export function setToken(token: string): void {
  memoryToken = token
  safeSetItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  memoryToken = null
  safeRemoveItem(TOKEN_KEY)
  safeRemoveItem(USER_KEY)
}

/** 让 401 处理逻辑可以被 router 接管，避免 api -> router 的循环依赖 */
let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler
}

export function handleUnauthorized(): void {
  clearToken()
  if (unauthorizedHandler) {
    unauthorizedHandler()
    return
  }
  if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
    window.location.href = '/login'
  }
}

/* ---------------------------- 错误归一化 ---------------------------- */

export interface NormalizedError {
  status: number | null
  message: string
  unauthorized: boolean
  raw: unknown
}

function flattenDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts: string[] = []
    for (const entry of detail) {
      if (entry && typeof entry === 'object' && 'msg' in entry) {
        const msg = (entry as { msg?: unknown }).msg
        if (typeof msg === 'string') parts.push(msg)
      }
    }
    if (parts.length > 0) return parts.join('；')
  }
  return null
}

/**
 * 后端不可达时的统一提示。
 *
 * 开发模式下 Vite 会把 /api 代理到 127.0.0.1:8000；后端没起来时，
 * 代理会返回 502/503/504，而 axios 自带的文案是
 * “Request failed with status code 502” —— 对使用者毫无指导意义
 * （真机上就出现过这个报错，排查成本很高）。这里换成可执行的提示。
 */
const BACKEND_UNREACHABLE_HINT =
  '无法连接后端服务。请确认已运行 start_backend.bat，且 8000 端口未被占用；' +
  '若使用单端口模式，请先运行 build_frontend.bat 再只启动后端。'

function isGatewayStatus(status: number | null): boolean {
  return status === 502 || status === 503 || status === 504
}

/**
 * 这个值是不是已经归一化过了？
 *
 * 判据用 `raw` 与 `unauthorized` 这两个**只有归一化结果才带**的字段 ——
 * axios 的 `AxiosError` 和普通 `Error` 都没有它们。
 */
function isNormalizedError(value: unknown): value is NormalizedError {
  return typeof value === 'object' && value !== null && 'raw' in value && 'unauthorized' in value
}

export function normalizeError(error: unknown): NormalizedError {
  // **必须幂等**：响应拦截器 reject 出去的已经是归一化结果，调用方在 catch 里
  // 再调一次 `normalizeError` 是很自然的写法。而真要落到下面那句 `String(error)`，
  // 会得到字面的 "[object Object]" —— 状态码也一起丢了，于是 `status === 404`
  // 永远为假。这以前真实发生过：设备详情页的"设备不存在"分支从不触发，
  // 实时画面上的故障原因显示成 "[object Object]"。
  if (isNormalizedError(error)) return error
  if (axios.isAxiosError(error)) {
    const status = error.response?.status ?? null
    const payload = error.response?.data as ApiErrorShape | undefined
    const fromDetail = payload ? flattenDetail(payload.detail) : null
    // 顺序很重要：后端自己给的 detail 最具体，优先展示；
    // 只有拿不到 detail 时才退到网关类提示。
    const message =
      fromDetail ??
      (typeof payload?.message === 'string' ? payload.message : null) ??
      (isGatewayStatus(status)
        ? `${BACKEND_UNREACHABLE_HINT}（HTTP ${status}）`
        : error.code === 'ECONNABORTED'
          ? '请求超时，请检查后端服务是否运行'
          : error.request && !error.response
            ? BACKEND_UNREACHABLE_HINT
            : error.message || '请求失败')
    return { status, message, unauthorized: status === 401, raw: error }
  }
  if (error instanceof Error) {
    return { status: null, message: error.message, unauthorized: false, raw: error }
  }
  return { status: null, message: String(error), unauthorized: false, raw: error }
}

/* ---------------------------- axios 实例 ---------------------------- */

/** API 前缀。全项目只此一处定义，axios 与 raw XHR 都从这里取。 */
/**
 * 接口前缀。导出是为了让**拼在 `<img src>` 里的地址**（如实时画面流）也能用同一份值 ——
 * 否则 `/api` 会在第二处硬编码，前缀一旦变动就会有地方悄悄坏掉。
 */
export const API_BASE = '/api'

/**
 * 给相对路径补上 API 前缀。
 *
 * **为什么必须要有它**：`upload()` 为了拿到上传进度事件而使用原生
 * XMLHttpRequest，因此**绕过了 axios 的 baseURL**。若这里不补前缀，
 * `upload('/detection/image')` 会真的 POST 到 `/detection/image` —— 该路径
 * 不存在，最终落到 SPA 回退路由上得到 `405 Method Not Allowed`。
 * 真机上就是因此翻车的（页面只显示 "Method Not Allowed"，极难定位）。
 */
function withApiBase(url: string): string {
  if (/^https?:\/\//i.test(url)) return url
  const base = API_BASE.replace(/\/+$/, '')
  if (url === base || url.startsWith(`${base}/`)) return url
  return base + (url.startsWith('/') ? url : `/${url}`)
}

const instance: AxiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: 120_000,
  headers: { Accept: 'application/json' }
})

instance.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  return config
})

instance.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error: unknown) => {
    const normalized = normalizeError(error)
    if (normalized.unauthorized) {
      handleUnauthorized()
    }
    return Promise.reject(normalized)
  }
)

export { instance as http }

/* ---------------------------- JSON 请求 ---------------------------- */

export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await instance.request<T>(config)
  return response.data
}

export function get<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return request<T>({ url, method: 'GET', params })
}

export function post<T>(url: string, data?: unknown): Promise<T> {
  return request<T>({ url, method: 'POST', data })
}

export function del<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return request<T>({ url, method: 'DELETE', params })
}

export function put<T>(url: string, data?: unknown): Promise<T> {
  return request<T>({ url, method: 'PUT', data })
}

/**
 * PATCH —— 局部更新。
 *
 * 与 `put` 的区别不只是动词：后端用 PATCH 的接口（如 `PATCH /api/devices/{id}`）
 * 靠 pydantic 的 `model_fields_set` 区分「没传这个字段」和「显式传了 null」，
 * 例如 `plot_id: null` 表示取消地块归属，而字段缺席表示不动它。
 * 所以调用方**该传什么就传什么，不要在这里补默认值**。
 */
export function patch<T>(url: string, data?: unknown): Promise<T> {
  return request<T>({ url, method: 'PATCH', data })
}

/* ---------------------------- 上传（带进度） ---------------------------- */

export interface UploadOptions {
  /** 数值 0~1 */
  onProgress?: (ratio: number) => void
  signal?: AbortSignal
  timeoutMs?: number
}

/**
 * multipart/form-data 上传。优先走 axios（能拿到上传进度），
 * 若环境不支持 XMLHttpRequest 上传事件则自动退化为 fetch。
 */
export function upload<T>(
  url: string,
  form: FormData,
  options: UploadOptions = {}
): Promise<T> {
  const { onProgress, signal, timeoutMs = 600_000 } = options
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', withApiBase(url), true)
    xhr.timeout = timeoutMs
    const token = getToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.setRequestHeader('Accept', 'application/json')

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.total > 0 ? event.loaded / event.total : 0)
      }
    }

    xhr.onload = () => {
      const contentType = xhr.getResponseHeader('Content-Type') ?? ''
      let payload: unknown = null
      if (contentType.includes('application/json')) {
        try {
          payload = JSON.parse(xhr.responseText) as unknown
        } catch {
          payload = null
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(1)
        resolve((payload ?? null) as T)
        return
      }
      if (xhr.status === 401) handleUnauthorized()
      const detail = (payload as ApiErrorShape | null)?.detail
      reject({
        status: xhr.status,
        message:
          flattenDetail(detail) ??
          (payload as ApiErrorShape | null)?.message ??
          (isGatewayStatus(xhr.status)
            ? `${BACKEND_UNREACHABLE_HINT}（HTTP ${xhr.status}）`
            : `上传失败（HTTP ${xhr.status}）`),
        unauthorized: xhr.status === 401,
        raw: payload
      } satisfies NormalizedError)
    }

    xhr.onerror = () => {
      reject({
        status: null,
        message: '上传过程中网络中断',
        unauthorized: false,
        raw: null
      } satisfies NormalizedError)
    }

    xhr.ontimeout = () => {
      reject({
        status: null,
        message: '上传超时',
        unauthorized: false,
        raw: null
      } satisfies NormalizedError)
    }

    xhr.onabort = () => {
      reject({
        status: null,
        message: '上传已取消',
        unauthorized: false,
        raw: null
      } satisfies NormalizedError)
    }

    if (signal) {
      if (signal.aborted) {
        xhr.abort()
        return
      }
      signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }

    xhr.send(form)
  })
}

/* ---------------------------- 文件下载 ---------------------------- */

export interface DownloadedFile {
  blob: Blob
  filename: string
}

/** 从 Content-Disposition 解析文件名，支持 filename*=UTF-8''xxx */
export function parseContentDisposition(header: string | undefined | null, fallback: string): string {
  if (!header) return fallback
  const star = /filename\*=(?:UTF-8|utf-8)''([^;]+)/i.exec(header)
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))
    } catch {
      return star[1].trim()
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header)
  if (plain?.[1]) {
    const value = plain[1].trim()
    try {
      return decodeURIComponent(value)
    } catch {
      return value
    }
  }
  return fallback
}

export async function download(
  url: string,
  params: Record<string, unknown> | undefined,
  fallbackFilename: string
): Promise<DownloadedFile> {
  const response = await instance.request<Blob>({
    url,
    method: 'GET',
    params,
    responseType: 'blob',
    timeout: 300_000
  })
  const disposition =
    (response.headers['content-disposition'] as string | undefined) ??
    (response.headers['Content-Disposition'] as string | undefined)
  return {
    blob: response.data,
    filename: parseContentDisposition(disposition, fallbackFilename)
  }
}

/** 触发浏览器下载 */
export function saveBlob(blob: Blob, filename: string): void {
  const href = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = href
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  // 交给浏览器读取后再释放
  window.setTimeout(() => URL.revokeObjectURL(href), 4000)
}

/** 把 Detection 的 bbox 与后端字段对齐（防御性：后端偶尔可能返回字符串数字） */
export function toNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const parsed = Number.parseFloat(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

export type { AxiosError }
