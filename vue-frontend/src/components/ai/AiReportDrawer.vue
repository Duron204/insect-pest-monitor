<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElButton, ElDrawer, ElRadioButton, ElRadioGroup } from 'element-plus'
import AiMessageContent from '@/components/ai/AiMessageContent.vue'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import { downloadReport, getReport, listReports, streamReport } from '@/api/ai'
import { notifyApiError } from '@/utils/notify'
import type { AiReportSummaryOut } from '@/types'

/**
 * 统计看板上的"AI 诊断报告"抽屉。
 *
 * 放在看板而不是 AI 助手页的原因：报告的输入就是看板正在看的数据，
 * "看完数据 → 生成报告"是同一段工作流；生成走 SSE 流式（报告较长，
 * 等整份返回的空白感会很差），完成后落库为快照并可导出 Word。
 */
const visible = defineModel<boolean>({ default: false })

const rangeToken = ref<'7d' | '30d'>('7d')
const generating = ref(false)
const content = ref('')
const activeReportId = ref<number | null>(null)
const error = ref<string | null>(null)
const reports = ref<AiReportSummaryOut[]>([])
const exporting = ref(false)

const canExport = computed(() => activeReportId.value !== null && !generating.value)

watch(visible, (open) => {
  if (open) void loadReports()
})

async function loadReports(): Promise<void> {
  try {
    reports.value = await listReports()
  } catch {
    reports.value = []
  }
}

async function generate(): Promise<void> {
  if (generating.value) return
  generating.value = true
  content.value = ''
  activeReportId.value = null
  error.value = null
  try {
    const result = await streamReport(rangeToken.value, (delta) => {
      content.value += delta
    })
    activeReportId.value = result.reportId
    void loadReports()
  } catch (err) {
    error.value = notifyApiError(err) || '报告生成失败'
  } finally {
    generating.value = false
  }
}

async function openReport(id: number): Promise<void> {
  if (generating.value) return
  try {
    const report = await getReport(id)
    activeReportId.value = id
    content.value = report.content
    rangeToken.value = report.range_token === '30d' ? '30d' : '7d'
    error.value = null
  } catch (err) {
    error.value = notifyApiError(err)
  }
}

async function exportDocx(): Promise<void> {
  if (activeReportId.value === null || exporting.value) return
  exporting.value = true
  try {
    await downloadReport(activeReportId.value)
  } catch (err) {
    error.value = notifyApiError(err) || '导出失败'
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <ElDrawer
    v-model="visible"
    title="AI 虫情诊断报告"
    size="min(560px, 92vw)"
    :close-on-click-modal="!generating"
  >
    <div class="ai-report">
      <div class="ai-report__controls">
        <ElRadioGroup v-model="rangeToken" size="small" :disabled="generating">
          <ElRadioButton value="7d">近 7 天</ElRadioButton>
          <ElRadioButton value="30d">近 30 天</ElRadioButton>
        </ElRadioGroup>
        <ElButton
          type="primary"
          size="small"
          :loading="generating"
          aria-label="生成诊断报告"
          @click="generate"
        >
          {{ generating ? '生成中…' : '生成报告' }}
        </ElButton>
        <ElButton
          size="small"
          :disabled="!canExport"
          :loading="exporting"
          aria-label="导出 Word 文档"
          @click="exportDocx"
        >
          导出 Word
        </ElButton>
      </div>

      <p class="ai-report__hint">
        报告基于实时检测数据与知识库生成，数字与告警均为真实数据；生成后自动存档。
      </p>

      <ErrorBanner :message="error" title="报告出错" @close="error = null" />

      <div class="ai-report__body" aria-live="polite">
        <AiMessageContent v-if="content" :content="content" />
        <p v-else-if="generating" class="ai-report__placeholder">正在撰写报告…</p>
        <p v-else class="ai-report__placeholder">
          点击「生成报告」，基于当前账号的检测数据与虫害知识库产出一份可导出的诊断报告。
        </p>
      </div>

      <section v-if="reports.length > 0" class="ai-report__history">
        <h3 class="ai-report__history-title">历史报告</h3>
        <ul class="ai-report__history-list">
          <li v-for="report in reports" :key="report.id">
            <button
              class="ai-report__history-item"
              :class="{ 'is-active': report.id === activeReportId }"
              type="button"
              @click="openReport(report.id)"
            >
              <span class="ai-report__history-name">{{ report.title }}</span>
              <span class="ai-report__history-meta">
                {{ report.created_at.slice(0, 16).replace('T', ' ') }} ·
                {{ report.total_tokens ?? 0 }} tokens
              </span>
            </button>
          </li>
        </ul>
      </section>
    </div>
  </ElDrawer>
</template>

<style scoped lang="scss">
.ai-report {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;

  &__controls {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  &__hint {
    margin: 0;
    font-size: var(--ip-fs-xs);
    line-height: 1.6;
    color: var(--ip-text-3);
  }

  &__body {
    flex: 1 1 auto;
    min-height: 200px;
    padding: 14px;
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius);
    background: var(--ip-panel-2);
    overflow-y: auto;
  }

  &__placeholder {
    margin: 24px 0;
    font-size: var(--ip-fs-sm);
    color: var(--ip-text-3);
    text-align: center;
  }

  &__history {
    flex: 0 0 auto;

    &-title {
      margin: 0 0 6px;
      font-size: var(--ip-fs-sm);
      font-weight: 600;
      color: var(--ip-text);
    }

    &-list {
      display: flex;
      flex-direction: column;
      gap: 2px;
      margin: 0;
      padding: 0;
      list-style: none;
      max-height: 180px;
      overflow-y: auto;
    }

    &-item {
      display: flex;
      flex-direction: column;
      gap: 2px;
      width: 100%;
      padding: 8px 10px;
      border: none;
      border-radius: var(--ip-radius-sm);
      background: transparent;
      text-align: left;
      cursor: pointer;
      transition: background var(--ip-dur-fast) var(--ip-ease);

      &:hover {
        background: var(--ip-primary-weak);
      }

      &.is-active {
        background: var(--ip-primary-weak);
        box-shadow: inset 2px 0 0 0 var(--ip-primary);
      }
    }

    &-name {
      font-size: var(--ip-fs-xs);
      color: var(--ip-text);
    }

    &-meta {
      font-size: 11px;
      color: var(--ip-text-3);
      font-family: var(--ip-font-mono);
    }
  }
}
</style>
