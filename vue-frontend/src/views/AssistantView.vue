<script setup lang="ts">
import { computed, nextTick, ref, useTemplateRef } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton } from 'element-plus'
import {
  deleteConversation,
  fetchAiStatus,
  fetchUsageSummary,
  getConversationMessages,
  listConversations,
  streamAiChat
} from '@/api/ai'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import AiMessageContent from '@/components/ai/AiMessageContent.vue'
import { confirmAction, notifyApiError, notifySuccess } from '@/utils/notify'
import type {
  AiCitationOut,
  AiChatHistoryMessage,
  AiConversationOut,
  AiStatusOut,
  AiUsageSummaryOut
} from '@/types'

interface ChatItem extends AiChatHistoryMessage {
  citations?: AiCitationOut[]
  elapsedMs?: number
  /** 流中途出错：保留已生成内容并标注不完整 */
  incomplete?: boolean
}

const router = useRouter()
const messages = ref<ChatItem[]>([])
const draft = ref('')
const streaming = ref(false)
const error = ref<string | null>(null)
const status = ref<AiStatusOut | null>(null)
const statusLoaded = ref(false)
const listEl = useTemplateRef<HTMLElement>('messageList')

// ---- 会话历史与用量 ----
const conversations = ref<AiConversationOut[]>([])
const activeConversationId = ref<number | null>(null)
const usage = ref<AiUsageSummaryOut | null>(null)

function formatRelativeTime(iso: string): string {
  const date = new Date(iso)
  const diffMs = Date.now() - date.getTime()
  const minutes = Math.floor(diffMs / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

async function loadConversations(): Promise<void> {
  try {
    conversations.value = await listConversations()
  } catch {
    conversations.value = []
  }
}

async function loadUsage(): Promise<void> {
  try {
    usage.value = await fetchUsageSummary()
  } catch {
    usage.value = null
  }
}

async function openConversation(id: number): Promise<void> {
  if (streaming.value || id === activeConversationId.value) return
  try {
    const rows = await getConversationMessages(id)
    activeConversationId.value = id
    messages.value = rows.map((row) => ({
      role: row.role,
      content: row.content,
      citations: row.citations,
      elapsedMs: row.elapsed_ms ?? undefined
    }))
    error.value = null
    scrollToBottom()
  } catch (err) {
    error.value = notifyApiError(err)
  }
}

function newConversation(): void {
  if (streaming.value) return
  activeConversationId.value = null
  messages.value = []
  error.value = null
  draft.value = ''
}

async function removeConversation(id: number): Promise<void> {
  const ok = await confirmAction({
    title: '删除会话',
    message: '删除后该会话的全部消息与用量记录一并移除，不可恢复。',
    confirmText: '删除',
    danger: true
  })
  if (!ok) return
  try {
    await deleteConversation(id)
    if (id === activeConversationId.value) newConversation()
    await loadConversations()
    notifySuccess('会话已删除')
  } catch (err) {
    error.value = notifyApiError(err)
  }
}

loadConversations()
loadUsage()

const suggestions = [
  { icon: 'leaf', text: '近一周虫情怎么样，需要打药吗？' },
  { icon: 'bug', text: '白菜心叶被虫蛀了怎么办？' },
  { icon: 'bell', text: '有哪些告警还没处理？' }
]

const aiAvailable = computed(
  () => status.value?.enabled === true && status.value?.configured === true
)
const historyForRequest = computed<AiChatHistoryMessage[]>(() =>
  messages.value.map(({ role, content }) => ({ role, content }))
)

function scrollToBottom(): void {
  void nextTick(() => {
    const el = listEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function ask(question: string): Promise<void> {
  const clean = question.trim()
  if (clean.length === 0 || streaming.value) return

  error.value = null
  messages.value.push({ role: 'user', content: clean })
  const assistant: ChatItem = { role: 'assistant', content: '' }
  messages.value.push(assistant)
  draft.value = ''
  streaming.value = true
  scrollToBottom()

  try {
    const result = await streamAiChat({
      question: clean,
      conversationId: activeConversationId.value,
      history: historyForRequest.value.slice(0, -1),
      onDelta: (text) => {
        assistant.content += text
        scrollToBottom()
      }
    })
    assistant.elapsedMs = result.elapsedMs
    assistant.citations = result.citations
    if (result.conversationId !== null) {
      activeConversationId.value = result.conversationId
    }
  } catch (err) {
    const message = notifyApiError(err) || 'AI 助手暂时不可用'
    if ((err as { status?: number | null })?.status === 503) {
      // 未配置：这是"功能没开"，给出指引比报错更有用
      error.value = message
      assistant.content = assistant.content || '（AI 助手尚未配置，见下方指引）'
      assistant.incomplete = true
    } else {
      assistant.content = assistant.content || '（本次回答未能完成）'
      assistant.incomplete = true
      error.value = message
    }
  } finally {
    streaming.value = false
    scrollToBottom()
    void loadStatus()
    void loadConversations()
    void loadUsage()
  }
}

function onSuggest(text: string): void {
  void ask(text)
}

function onEnter(event: KeyboardEvent): void {
  if (event.shiftKey) return
  event.preventDefault()
  void ask(draft.value)
}

async function loadStatus(): Promise<void> {
  try {
    status.value = await fetchAiStatus()
  } catch {
    status.value = null
  } finally {
    statusLoaded.value = true
  }
}
loadStatus()

function citationLabel(c: AiCitationOut): string {
  return `${c.zh_name} · ${c.field_label}`
}

function openCitation(c: AiCitationOut): void {
  void router.push({ name: 'species', query: { class: String(c.class_id) } })
}

/** 行内 [n] 引用点击：回到该消息的引用徽标再跳转（没有则忽略） */
function onInlineCite(message: ChatItem, n: number): void {
  const citation = message.citations?.find((c) => c.index === n)
  if (citation) openCitation(citation)
}
</script>

<template>
  <div class="assistant-view">
    <header class="assistant-view__bar ip-panel">
      <div class="assistant-view__bar-left">
        <h2 class="ip-section-title">AI 植保助手</h2>
        <span class="assistant-view__subtitle">
          基于知识库检索 + 实时虫情数据回答，结论带引用
        </span>
      </div>
      <div class="assistant-view__bar-right">
        <!-- 用量观测：调用数与 token 一眼可见（不做配额，只做透明） -->
        <span v-if="usage && usage.calls > 0" class="ip-chip assistant-view__chip--usage" role="status">
          {{ usage.calls }} 次调用 · {{ usage.total_tokens.toLocaleString() }} tokens
        </span>
        <span
          v-if="statusLoaded && status"
          class="ip-chip"
          :class="aiAvailable ? 'assistant-view__chip--ok' : 'assistant-view__chip--off'"
          role="status"
        >
          <span class="ip-dot ip-dot--ok" v-if="aiAvailable" aria-hidden="true" />
          <span class="ip-dot ip-dot--warn" v-else aria-hidden="true" />
          {{ aiAvailable ? `已就绪 · ${status.model}` : '未配置' }}
        </span>
      </div>
    </header>

    <!-- 未配置指引：不是报错弹窗，而是"怎么开"的步骤 -->
    <section
      v-if="statusLoaded && status && !aiAvailable"
      class="assistant-view__guide ip-panel"
      aria-label="AI 助手配置指引"
    >
      <h3 class="assistant-view__guide-title">开启 AI 助手（两步）</h3>
      <ol class="assistant-view__guide-steps">
        <li>
          确认 <code>config.yaml</code> 的 <code>ai</code> 段已填
          <code>base_url</code> 且 <code>enabled: true</code>
        </li>
        <li>
          设置环境变量 <code>INSECT_AI_API_KEY</code> 为模型服务密钥（智谱 / DeepSeek
          等 OpenAI 兼容服务均可），重启后端生效
        </li>
      </ol>
      <p class="assistant-view__guide-note">
        未配置不影响检测、统计与其他功能；向量检索不可用时自动降级为词法检索。
      </p>
    </section>

    <ErrorBanner :message="error" title="AI 助手出错" @close="error = null" />

    <div class="assistant-view__body">
      <!-- 会话列表：历史即资产（含用量记账），刷新页面不再丢 -->
      <aside class="assistant-view__sessions ip-panel" aria-label="历史会话">
        <div class="assistant-view__sessions-head">
          <span class="assistant-view__sessions-title">历史会话</span>
          <ElButton size="small" text aria-label="开始新对话" @click="newConversation">
            新对话
          </ElButton>
        </div>
        <ul class="assistant-view__sessions-list">
          <li v-for="conversation in conversations" :key="conversation.id">
            <button
              class="assistant-view__session"
              :class="{ 'is-active': conversation.id === activeConversationId }"
              type="button"
              @click="openConversation(conversation.id)"
            >
              <span class="assistant-view__session-title">{{ conversation.title || '（未命名会话）' }}</span>
              <span class="assistant-view__session-meta">
                {{ conversation.message_count }} 条 · {{ formatRelativeTime(conversation.updated_at) }}
              </span>
            </button>
            <button
              class="assistant-view__session-del"
              type="button"
              :aria-label="`删除会话：${conversation.title || '未命名'}`"
              @click.stop="removeConversation(conversation.id)"
            >
              ×
            </button>
          </li>
        </ul>
        <p v-if="conversations.length === 0" class="assistant-view__sessions-empty">
          还没有历史会话
        </p>
      </aside>

      <section class="assistant-view__chat ip-panel">
      <!-- 空状态：说明能问什么，而不是一片空白 -->
      <div v-if="messages.length === 0" class="assistant-view__empty">
        <svg viewBox="0 0 120 96" width="132" height="106" aria-hidden="true" class="assistant-view__empty-art">
          <defs>
            <linearGradient id="leaf-grad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="var(--ip-primary)" stop-opacity="0.9" />
              <stop offset="1" stop-color="var(--ip-primary)" stop-opacity="0.35" />
            </linearGradient>
          </defs>
          <path d="M18 84c14-6 26-16 33-30M51 54C38 44 34 30 38 16c14 4 22 16 21 30M51 54c10-8 22-10 34-6-6 12-18 19-31 19"
            fill="none" stroke="url(#leaf-grad)" stroke-width="3" stroke-linecap="round" />
          <circle cx="88" cy="20" r="3" fill="var(--ip-primary)" />
          <path d="M88 26v6M82 23h-6M94 23h6M83.5 30.5l-4 4M92.5 30.5l4 4" stroke="var(--ip-primary)" stroke-width="1.6" stroke-linecap="round" />
          <path d="M20 30h4M22 28v4M104 58h3M105.5 56.5v3" stroke="var(--ip-primary)" stroke-width="1.4" stroke-linecap="round" opacity="0.7" />
        </svg>
        <h3 class="assistant-view__empty-title">问点什么吧</h3>
        <p class="assistant-view__empty-text">
          助手会同时参考 <b>15 类虫害知识库</b>、<b>近 7 天检测数据</b> 与
          <b>未确认告警</b> 回答，并在结论后标注引用来源。
        </p>
        <div class="assistant-view__suggests">
          <button
            v-for="item in suggestions"
            :key="item.text"
            class="assistant-view__suggest"
            type="button"
            @click="onSuggest(item.text)"
          >
            {{ item.text }}
          </button>
        </div>
      </div>

      <!-- 消息流 -->
      <div v-else ref="messageList" class="assistant-view__messages" aria-live="polite">
        <article
          v-for="(message, i) in messages"
          :key="i"
          class="assistant-view__row"
          :class="message.role === 'user' ? 'assistant-view__row--user' : 'assistant-view__row--ai'"
        >
          <span class="assistant-view__avatar" :class="`assistant-view__avatar--${message.role}`" aria-hidden="true">
            <svg v-if="message.role === 'user'" viewBox="0 0 20 20" width="14" height="14">
              <circle cx="10" cy="7" r="3" fill="none" stroke="currentColor" stroke-width="1.5" />
              <path d="M4 17c1.4-3.4 4-5 6-5s4.6 1.6 6 5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            <svg v-else viewBox="0 0 20 20" width="14" height="14">
              <path d="M10 17c0-4 1.4-7.2 4.8-9.6M10 17c0-4-1.4-7.2-4.8-9.6M10 17v-4.8"
                fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
              <path d="M10 7.2c2 0 3.6-1.3 4.3-3.3C12.4 3.3 10.8 4.6 10 6.7M10 7.2C8 7.2 6.4 5.9 5.7 3.9 7.6 3.3 9.2 4.6 10 6.7"
                fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
          </span>

          <div class="assistant-view__bubble-wrap">
            <div class="assistant-view__bubble" :class="{ 'assistant-view__bubble--incomplete': message.incomplete }">
              <AiMessageContent
              v-if="message.content.length > 0"
              :content="message.content"
              @cite="(n: number) => onInlineCite(message, n)"
            />
              <span v-else-if="streaming" class="assistant-view__typing" aria-label="正在生成回答">
                <i /><i /><i />
              </span>
              <span v-else class="assistant-view__empty-answer">（无内容）</span>
            </div>

            <!-- 引用徽标：可点击跳知识库卡片 -->
            <div v-if="message.citations && message.citations.length > 0" class="assistant-view__citations">
              <span class="assistant-view__citations-label">引用来源</span>
              <button
                v-for="citation in message.citations"
                :key="citation.index"
                class="assistant-view__citation"
                type="button"
                :aria-label="`查看 ${citationLabel(citation)} 知识卡`"
                @click="openCitation(citation)"
              >
                <span class="assistant-view__citation-index">{{ citation.index }}</span>
                {{ citationLabel(citation) }}
              </button>
            </div>
          </div>
        </article>
      </div>

      <!-- 输入区 -->
      <div class="assistant-view__composer">
        <textarea
          v-model="draft"
          class="assistant-view__input"
          rows="2"
          maxlength="500"
          placeholder="例如：这周虫情怎么样？叶子上有白色小虫怎么处理？"
          aria-label="输入问题"
          :disabled="streaming"
          @keydown="onEnter"
        />
        <div class="assistant-view__composer-side">
          <span class="assistant-view__count">{{ draft.length }}/500</span>
          <ElButton
            type="primary"
            :loading="streaming"
            :disabled="draft.trim().length === 0"
            aria-label="发送问题"
            @click="ask(draft)"
          >
            发送
          </ElButton>
        </div>
      </div>
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
.assistant-view {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  flex: 1 1 auto;
  min-height: 0;

  &__bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ip-gap);
    padding: 12px 16px;
    flex: 0 0 auto;
  }

  &__bar-left {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  &__subtitle {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__chip--ok {
    background: var(--ip-primary-weak);
    color: var(--ip-text);
  }

  &__chip--off {
    background: var(--ip-warn-weak);
    color: var(--ip-warn);
  }

  /* ---- 未配置指引 ---- */
  &__guide {
    padding: 14px 16px;
    background: var(--ip-warn-weak);
    border-color: var(--ip-warn-line);
    flex: 0 0 auto;
  }

  &__guide-title {
    margin: 0 0 6px;
    font-size: var(--ip-fs-sm);
    font-weight: 600;
    color: var(--ip-warn);
  }

  &__guide-steps {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 0;
    padding-left: 18px;
    font-size: var(--ip-fs-xs);
    line-height: 1.7;
    color: var(--ip-text-2);

    code {
      padding: 1px 5px;
      border-radius: 4px;
      background: var(--ip-panel-3);
      border: 1px solid var(--ip-line);
      font-family: var(--ip-font-mono);
      font-size: 11px;
      color: var(--ip-text);
    }
  }

  &__guide-note {
    margin: 8px 0 0;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  /* ---- 聊天主区 ---- */
  &__chat {
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
  }

  &__empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    flex: 1 1 auto;
    padding: 32px 24px;
    text-align: center;
  }

  &__empty-art {
    margin-bottom: 4px;
  }

  &__empty-title {
    font-size: var(--ip-fs-title);
    font-weight: 600;
    color: var(--ip-text);
  }

  &__empty-text {
    max-width: 520px;
    font-size: var(--ip-fs-sm);
    line-height: 1.7;
    color: var(--ip-text-2);

    b {
      color: var(--ip-text);
    }
  }

  &__suggests {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px;
    margin-top: 8px;
  }

  &__suggest {
    padding: 8px 14px;
    border: 1px solid var(--ip-line);
    border-radius: 999px;
    background: var(--ip-panel-2);
    color: var(--ip-text-2);
    font-size: var(--ip-fs-sm);
    cursor: pointer;
    transition:
      border-color var(--ip-dur-fast) var(--ip-ease),
      color var(--ip-dur-fast) var(--ip-ease),
      background var(--ip-dur-fast) var(--ip-ease),
      transform var(--ip-dur-fast) var(--ip-ease);

    &:hover {
      border-color: var(--ip-primary-line);
      background: var(--ip-primary-weak);
      color: var(--ip-text);
      transform: translateY(-1px);
    }

    &:active {
      transform: translateY(0);
    }
  }

  /* ---- 消息流 ---- */
  &__messages {
    display: flex;
    flex-direction: column;
    gap: 14px;
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 18px 18px 8px;
  }

  &__row {
    display: flex;
    gap: 10px;
    max-width: 86%;

    &--user {
      align-self: flex-end;
      flex-direction: row-reverse;
    }

    &--ai {
      align-self: flex-start;
    }
  }

  &__avatar {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    flex: 0 0 auto;
    border-radius: 50%;
    border: 1px solid var(--ip-line);

    &--user {
      background: var(--ip-panel-3);
      color: var(--ip-text-2);
    }

    &--assistant {
      background: var(--ip-primary-weak);
      border-color: var(--ip-primary-line);
      color: var(--ip-primary);
    }
  }

  &__bubble-wrap {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  &__bubble {
    padding: 10px 14px;
    border-radius: var(--ip-radius-lg);
    background: var(--ip-panel-2);
    border: 1px solid var(--ip-line-soft);
    min-width: 40px;

    .assistant-view__row--user & {
      background: var(--ip-primary-weak);
      border-color: var(--ip-primary-line);
    }

    &--incomplete {
      border-style: dashed;
      border-color: var(--ip-warn-line);
    }
  }

  &__empty-answer {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  /* 打字指示：三个点交替呼吸 */
  &__typing {
    display: inline-flex;
    gap: 4px;
    padding: 4px 2px;

    i {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--ip-text-3);
      animation: ip-ai-typing 1.2s var(--ip-ease) infinite;

      &:nth-child(2) {
        animation-delay: 0.15s;
      }

      &:nth-child(3) {
        animation-delay: 0.3s;
      }
    }
  }

  @keyframes ip-ai-typing {
    0%,
    100% {
      opacity: 0.3;
      transform: translateY(0);
    }
    50% {
      opacity: 1;
      transform: translateY(-2px);
    }
  }

  &__citations {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }

  &__citations-label {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__citation {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 24px;
    padding: 0 10px 0 6px;
    border: 1px solid var(--ip-line);
    border-radius: 999px;
    background: var(--ip-panel-2);
    color: var(--ip-text-2);
    font-size: var(--ip-fs-xs);
    cursor: pointer;
    transition:
      border-color var(--ip-dur-fast) var(--ip-ease),
      color var(--ip-dur-fast) var(--ip-ease);

    &:hover {
      border-color: var(--ip-primary-line);
      color: var(--ip-text);
    }
  }

  &__citation-index {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--ip-primary-weak);
    color: var(--ip-primary);
    font-family: var(--ip-font-mono);
    font-weight: 600;
  }

  /* ---- 输入区 ---- */
  &__composer {
    display: flex;
    gap: 10px;
    align-items: flex-end;
    padding: 12px 14px;
    border-top: 1px solid var(--ip-line-soft);
    flex: 0 0 auto;
  }

  &__input {
    flex: 1 1 auto;
    min-height: 56px;
    max-height: 120px;
    padding: 10px 12px;
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius);
    background: var(--ip-panel-2);
    color: var(--ip-text);
    font-family: inherit;
    font-size: var(--ip-fs-sm);
    line-height: 1.6;
    resize: none;
    transition: border-color var(--ip-dur-fast) var(--ip-ease);

    &:focus {
      outline: none;
      border-color: var(--ip-primary-line);
      box-shadow: 0 0 0 2px var(--ip-primary-weak);
    }

    &:disabled {
      opacity: 0.6;
    }

    &::placeholder {
      color: var(--ip-text-3);
    }
  }

  &__composer-side {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 6px;
    flex: 0 0 auto;
  }

  &__count {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    font-family: var(--ip-font-mono);
  }

  /* ---- 会话侧栏 + 主体双栏 ---- */
  &__body {
    display: flex;
    gap: var(--ip-gap);
    flex: 1 1 auto;
    min-height: 0;
  }

  &__chip--usage {
    background: var(--ip-panel-3);
    color: var(--ip-text-2);
    font-family: var(--ip-font-mono);
    font-size: var(--ip-fs-xs);
  }

  &__sessions {
    display: flex;
    flex-direction: column;
    width: 224px;
    flex: 0 0 auto;
    min-height: 0;
    overflow: hidden;
  }

  &__sessions-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 12px;
    border-bottom: 1px solid var(--ip-line-soft);
    flex: 0 0 auto;
  }

  &__sessions-title {
    font-size: var(--ip-fs-sm);
    font-weight: 600;
    color: var(--ip-text);
  }

  &__sessions-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 6px;
    overflow-y: auto;
    flex: 1 1 auto;
    min-height: 0;
  }

  &__sessions-list li {
    display: flex;
    align-items: stretch;
    gap: 2px;
  }

  &__session {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1 1 auto;
    min-width: 0;
    padding: 8px 10px;
    border: none;
    border-radius: var(--ip-radius-sm);
    background: transparent;
    text-align: left;
    cursor: pointer;
    transition:
      background var(--ip-dur-fast) var(--ip-ease),
      color var(--ip-dur-fast) var(--ip-ease);

    &:hover {
      background: var(--ip-primary-weak);
    }

    &.is-active {
      background: var(--ip-primary-weak);
      box-shadow: inset 2px 0 0 0 var(--ip-primary);
    }
  }

  &__session-title {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  &__session-meta {
    font-size: 11px;
    color: var(--ip-text-3);
    white-space: nowrap;
  }

  &__session-del {
    flex: 0 0 auto;
    width: 24px;
    border: none;
    border-radius: var(--ip-radius-sm);
    background: transparent;
    color: var(--ip-text-3);
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
    opacity: 0;
    transition:
      opacity var(--ip-dur-fast) var(--ip-ease),
      color var(--ip-dur-fast) var(--ip-ease);

    /* 只在行悬停时露出删除：默认一片 × 会像垃圾场 */
    .assistant-view__sessions-list li:hover & {
      opacity: 1;
    }

    &:hover {
      color: var(--ip-danger);
    }
  }

  &__sessions-empty {
    padding: 18px 14px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    text-align: center;
  }

  /* 主体聊天区在双栏下占满剩余宽度 */
  &__chat {
    flex: 1 1 auto;
  }

  /* 小尺寸笔记本：会话栏让位（与全站"只在电脑上用"的断点口径一致） */
  @media (max-width: 1279px) {
    &__sessions {
      display: none;
    }
  }
}
</style>
