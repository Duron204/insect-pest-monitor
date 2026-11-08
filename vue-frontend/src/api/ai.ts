import { API_BASE, del, get, getToken, handleUnauthorized, type NormalizedError } from './client'
import type {
  AiChatHistoryMessage,
  AiCitationOut,
  AiConversationOut,
  AiMessageOut,
  AiReportOut,
  AiReportSummaryOut,
  AiStatusOut,
  AiUsageSummaryOut
} from '@/types'

/* ---------------------------- AI 助手 ---------------------------- */

export interface StreamAiChatOptions {
  question: string
  /** 追问传已有会话 id；缺省新建 */
  conversationId?: number | null
  history?: AiChatHistoryMessage[]
  onDelta?: (text: string) => void
  signal?: AbortSignal
}

export interface StreamAiChatResult {
  answer: string
  elapsedMs: number
  citations: AiCitationOut[]
  conversationId: number | null
  usage: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number }
}

/**
 * 流式对话。
 *
 * **为什么不用 axios**：axios 在浏览器端不暴露流式读取
 * （onDownloadProgress 只给 chunk，无法精细处理 SSE 事件边界），
 * 这里直接 fetch + ReadableStream 手工解析。
 */
export async function streamAiChat(
  options: StreamAiChatOptions
): Promise<StreamAiChatResult> {
  const { question, conversationId: requestConversationId = null, history = [], onDelta, signal } = options

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream'
  }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(`${API_BASE}/ai/chat`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ question, history, conversation_id: requestConversationId }),
      signal
    })
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err
    throw {
      status: null,
      message: '无法连接后端服务，请确认后端已运行',
      unauthorized: false,
      raw: err
    } satisfies NormalizedError
  }

  if (response.status === 401) {
    handleUnauthorized()
    throw { status: 401, message: '登录已过期，请重新登录', unauthorized: true, raw: null } satisfies NormalizedError
  }

  // 未启用 / 未配置：后端返回 JSON 503（流开始前的失败用状态码，最诚实）
  if (!response.ok || !response.body) {
    let message = `AI 助手请求失败（HTTP ${response.status}）`
    try {
      const payload = (await response.json()) as { detail?: unknown }
      if (typeof payload.detail === 'string') message = payload.detail
    } catch {
      /* 保持默认文案 */
    }
    throw { status: response.status, message, unauthorized: false, raw: null } satisfies NormalizedError
  }

  // ---- 手工解析 SSE（事件以空行分隔，行以 data: 开头） ----
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let answer = ''
  let elapsedMs = 0
  let citations: AiCitationOut[] = []
  let conversationId: number | null = null
  const usage: StreamAiChatResult['usage'] = {}
  let streamError: string | null = null
  let finished = false

  const handleEvent = (raw: string): void => {
    const line = raw.trim()
    if (!line.startsWith('data:')) return
    let payload: unknown
    try {
      payload = JSON.parse(line.slice(5).trim())
    } catch {
      return // 半行/心跳噪声：跳过比中断整个回答划算
    }
    const event = payload as {
      type?: string
      text?: string
      message?: string
      elapsed_ms?: number
      conversation_id?: number
      usage?: StreamAiChatResult['usage']
    }
    if (event.type === 'meta' && typeof event.conversation_id === 'number') {
      conversationId = event.conversation_id
    } else if (event.type === 'delta' && typeof event.text === 'string') {
      answer += event.text
      onDelta?.(event.text)
    } else if (event.type === 'error' && typeof event.message === 'string') {
      // 流中途失败：已收到的内容保留，错误在 resolve 时抛出
      streamError = event.message
      finished = true
    } else if (event.type === 'done') {
      elapsedMs = typeof event.elapsed_ms === 'number' ? event.elapsed_ms : 0
      citations = Array.isArray((payload as { citations?: AiCitationOut[] }).citations)
        ? (payload as { citations: AiCitationOut[] }).citations
        : []
      if (event.usage && typeof event.usage === 'object') Object.assign(usage, event.usage)
      finished = true
    }
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let separator = buffer.indexOf('\n\n')
      while (separator !== -1) {
        handleEvent(buffer.slice(0, separator))
        buffer = buffer.slice(separator + 2)
        separator = buffer.indexOf('\n\n')
      }
      if (finished) {
        await reader.cancel().catch(() => undefined)
        break
      }
    }
    if (buffer.trim().length > 0) handleEvent(buffer)
  } finally {
    reader.releaseLock()
  }

  if (streamError !== null) {
    throw {
      status: null,
      message: streamError,
      unauthorized: false,
      raw: streamError
    } satisfies NormalizedError
  }
  if (!finished && answer.length === 0) {
    throw {
      status: null,
      message: '连接在回答开始前中断，请重试',
      unauthorized: false,
      raw: null
    } satisfies NormalizedError
  }

  return { answer, elapsedMs, citations, conversationId, usage }
}

export function fetchAiStatus(): Promise<AiStatusOut> {
  return get<AiStatusOut>('/ai/status')
}


/* ---------------------------- 会话历史 ---------------------------- */

export function listConversations(limit = 50): Promise<AiConversationOut[]> {
  return get<AiConversationOut[]>('/ai/conversations', { limit })
}

export function getConversationMessages(id: number): Promise<AiMessageOut[]> {
  return get<AiMessageOut[]>(`/ai/conversations/${id}`)
}

export function deleteConversation(id: number): Promise<void> {
  return del<void>(`/ai/conversations/${id}`)
}

/* ---------------------------- 用量观测 ---------------------------- */

export function fetchUsageSummary(days = 14): Promise<AiUsageSummaryOut> {
  return get<AiUsageSummaryOut>('/ai/usage/summary', { days })
}

/* ---------------------------- 诊断报告 ---------------------------- */

export interface ReportStreamResult {
  content: string
  reportId: number | null
  title: string
  elapsedMs: number
}

export async function streamReport(
  rangeToken: '7d' | '30d',
  onDelta?: (text: string) => void
): Promise<ReportStreamResult> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream'
  }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  const response = await fetch(`${API_BASE}/ai/report?range_token=${rangeToken}`, {
    method: 'POST',
    headers
  })
  if (response.status === 401) {
    handleUnauthorized()
    throw { status: 401, message: '登录已过期，请重新登录', unauthorized: true, raw: null } satisfies NormalizedError
  }
  if (!response.ok || !response.body) {
    let message = `报告生成失败（HTTP ${response.status}）`
    try {
      const payload = (await response.json()) as { detail?: unknown }
      if (typeof payload.detail === 'string') message = payload.detail
    } catch {
      /* 保持默认文案 */
    }
    throw { status: response.status, message, unauthorized: false, raw: null } satisfies NormalizedError
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let content = ''
  let reportId: number | null = null
  let title = ''
  let elapsedMs = 0
  let failed: string | null = null

  const handleEvent = (raw: string): void => {
    const line = raw.trim()
    if (!line.startsWith('data:')) return
    try {
      const payload = JSON.parse(line.slice(5).trim()) as {
        type?: string
        text?: string
        message?: string
        report_id?: number
        title?: string
        elapsed_ms?: number
      }
      if (payload.type === 'delta' && typeof payload.text === 'string') {
        content += payload.text
        onDelta?.(payload.text)
      } else if (payload.type === 'error' && typeof payload.message === 'string') {
        failed = payload.message
      } else if (payload.type === 'done') {
        reportId = typeof payload.report_id === 'number' ? payload.report_id : null
        title = payload.title ?? ''
        elapsedMs = payload.elapsed_ms ?? 0
      }
    } catch {
      /* 跳过半行 */
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let separator = buffer.indexOf('\n\n')
    while (separator !== -1) {
      handleEvent(buffer.slice(0, separator))
      buffer = buffer.slice(separator + 2)
      separator = buffer.indexOf('\n\n')
    }
  }
  if (buffer.trim().length > 0) handleEvent(buffer)
  reader.releaseLock()

  if (failed !== null) {
    throw { status: null, message: failed, unauthorized: false, raw: failed } satisfies NormalizedError
  }
  return { content, reportId, title, elapsedMs }
}

export function listReports(limit = 20): Promise<AiReportSummaryOut[]> {
  return get<AiReportSummaryOut[]>('/ai/report', { limit })
}

export function getReport(id: number): Promise<AiReportOut> {
  return get<AiReportOut>(`/ai/report/${id}`)
}

/** 导出 Word：fetch 带 Authorization 头拿 blob，再触发浏览器下载。
 * 不把 token 拼进 URL —— URL 会进服务器日志与浏览器历史。 */
export async function downloadReport(id: number): Promise<void> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(`${API_BASE}/ai/report/${id}/export`, { headers })
  if (!response.ok) {
    throw { status: response.status, message: `导出失败（HTTP ${response.status}）`, unauthorized: false, raw: null } satisfies NormalizedError
  }
  const blob = await response.blob()
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = disposition.match(/filename\*=UTF-8''([^;]+)/)
  const encoded = match?.[1]
  const filename = encoded ? decodeURIComponent(encoded) : `虫情诊断报告-${id}.docx`
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}
