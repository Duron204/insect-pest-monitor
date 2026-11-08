/**
 * AI 回答的轻量 Markdown 解析。
 *
 * **为什么不引入 marked / markdown-it**：
 * 1. 模型被 system prompt 约束为「短列表 + 少量加粗」，
 *    完整 Markdown 渲染器带来的是 XSS 面（v-html）与几十 KB 依赖；
 * 2. 解析结果输出**结构化 Segment 树**，由 Vue 模板渲染 ——
 *    内容天然被转义，不存在注入路径；
 * 3. 引用 [n] 需要做成可点击元素，v-html 方案反而做不了。
 */

export type InlineSegment =
  | { kind: 'text'; text: string }
  | { kind: 'bold'; text: string }
  | { kind: 'cite'; n: number }

export interface ContentBlock {
  type: 'paragraph' | 'list'
  /** 段落：单行；列表：多个条目 */
  lines: InlineSegment[][]
}

const INLINE_PATTERN = /(\*\*[^*\n]+\*\*|\[[0-9]{1,2}\])/g

export function parseInline(line: string): InlineSegment[] {
  const segments: InlineSegment[] = []
  let lastIndex = 0
  for (const match of line.matchAll(INLINE_PATTERN)) {
    const token = match[0]
    const index = match.index ?? 0
    if (index > lastIndex) {
      segments.push({ kind: 'text', text: line.slice(lastIndex, index) })
    }
    if (token.startsWith('**')) {
      segments.push({ kind: 'bold', text: token.slice(2, -2) })
    } else {
      segments.push({ kind: 'cite', n: Number(token.slice(1, -1)) })
    }
    lastIndex = index + token.length
  }
  if (lastIndex < line.length) {
    segments.push({ kind: 'text', text: line.slice(lastIndex) })
  }
  return segments.length > 0 ? segments : [{ kind: 'text', text: '' }]
}

const LIST_PREFIX = /^\s*(?:[-•·]\s+|\d{1,2}[.、)]\s*)/

function isListLine(line: string): boolean {
  return LIST_PREFIX.test(line)
}

function stripListMarker(line: string): string {
  return line.replace(LIST_PREFIX, '')
}

export function parseBlocks(content: string): ContentBlock[] {
  const normalized = (content ?? '').replace(/\r\n/g, '\n').trim()
  if (normalized.length === 0) return []

  const blocks: ContentBlock[] = []
  let paragraph: InlineSegment[][] = []
  let list: InlineSegment[][] = []

  const flushParagraph = (): void => {
    if (paragraph.length > 0) {
      blocks.push({ type: 'paragraph', lines: paragraph })
      paragraph = []
    }
  }
  const flushList = (): void => {
    if (list.length > 0) {
      blocks.push({ type: 'list', lines: list })
      list = []
    }
  }

  for (const rawLine of normalized.split('\n')) {
    const line = rawLine.trimEnd()
    if (line.trim().length === 0) {
      flushParagraph()
      flushList()
      continue
    }
    if (isListLine(line)) {
      flushParagraph()
      list.push(parseInline(stripListMarker(line)))
    } else {
      flushList()
      paragraph.push(parseInline(line))
    }
  }
  flushParagraph()
  flushList()
  return blocks
}
