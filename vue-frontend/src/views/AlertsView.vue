<script setup lang="ts">
/**
 * 告警中心。
 *
 * 这个页面本来是不存在的：后端早就会在每次任务完成后评估规则、把命中的规则
 * 写进 alerts 表，而且**树莓派本机抓拍同样会触发**（`services/local_camera.py`
 * 调用了 `evaluate_task`）—— 但前端没有任何入口，等于告警产生了没人看得见。
 *
 * 设计上两点刻意：
 *  1. 规则只**展示**、不在页面上改：阈值属于生产参数，放在
 *     `backend/app/data/alert_rules.yaml` 里可审计、可版本对比。
 *  2. 列表默认只看"未确认"，因为告警页面最常用的动作就是处理掉它。
 */
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElOption, ElSelect, ElTag, ElTooltip } from 'element-plus'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import { ackAlert, ackAllAlerts, listAlertRules, listAlerts } from '@/api/alerts'
import { useAlertsStore } from '@/stores/alerts'
import { formatDateTime, formatNumber, formatTaskNo } from '@/utils'
import { confirmAction, notifyApiError, notifySuccess } from '@/utils/notify'
import type { AlertOut, AlertRulesOut } from '@/types'

type StatusFilter = 'unacked' | 'all'

const alerts = useAlertsStore()

const status = ref<StatusFilter>('unacked')
const level = ref('')
const rows = ref<AlertOut[]>([])
const rules = ref<AlertRulesOut | null>(null)
const loading = ref(false)
const rulesLoading = ref(false)
const error = ref<string | null>(null)
const ackingId = ref<number | null>(null)
const ackingAll = ref(false)
const showRules = ref(false)

const LEVEL_OPTIONS = [
  { value: '', label: '全部级别' },
  { value: 'critical', label: '严重' },
  { value: 'warn', label: '警告' },
  { value: 'info', label: '提示' }
]

const STATUS_TABS: Array<{ value: StatusFilter; label: string }> = [
  { value: 'unacked', label: '未确认' },
  { value: 'all', label: '全部' }
]

const unackedCount = computed(() => rows.value.filter((row) => !row.acked).length)
const ruleCount = computed(() => rules.value?.rules?.length ?? 0)
const webhookOn = computed(() => rules.value?.webhook?.enabled === true)

function levelLabel(value: string): string {
  switch (value) {
    case 'critical':
      return '严重'
    case 'warn':
      return '警告'
    case 'info':
      return '提示'
    default:
      return value || '—'
  }
}

function levelTone(value: string): 'danger' | 'warning' | 'info' {
  switch (value) {
    case 'critical':
      return 'danger'
    case 'warn':
      return 'warning'
    default:
      return 'info'
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const query: { acked?: boolean; level?: string; limit: number } = { limit: 200 }
    if (status.value === 'unacked') query.acked = false
    if (level.value) query.level = level.value
    rows.value = await listAlerts(query)
    await alerts.refresh()
  } catch (err) {
    rows.value = []
    error.value = notifyApiError(err)
  } finally {
    loading.value = false
  }
}

async function loadRules(): Promise<void> {
  if (rules.value) return
  rulesLoading.value = true
  try {
    rules.value = await listAlertRules()
  } catch {
    // 规则拿不到不影响看告警本身
    rules.value = { rules: [] }
  } finally {
    rulesLoading.value = false
  }
}

async function toggleRules(): Promise<void> {
  showRules.value = !showRules.value
  if (showRules.value) await loadRules()
}

async function onAck(row: AlertOut): Promise<void> {
  ackingId.value = row.id
  try {
    const updated = await ackAlert(row.id)
    rows.value = rows.value.map((item) => (item.id === updated.id ? updated : item))
    if (status.value === 'unacked') {
      rows.value = rows.value.filter((item) => item.id !== updated.id)
    }
    await alerts.refresh()
    notifySuccess('已确认该条告警')
  } catch (err) {
    error.value = notifyApiError(err)
  } finally {
    ackingId.value = null
  }
}

async function onAckAll(): Promise<void> {
  const count = unackedCount.value
  if (count === 0) return
  const ok = await confirmAction({
    title: '全部确认',
    message: `确认把当前 ${count} 条未确认告警全部标记为已处理？`,
    confirmText: '全部确认'
  })
  if (!ok) return
  ackingAll.value = true
  try {
    const result = await ackAllAlerts()
    notifySuccess(`已确认 ${formatNumber(result.acked)} 条告警`)
    await load()
  } catch (err) {
    error.value = notifyApiError(err)
  } finally {
    ackingAll.value = false
  }
}

function switchStatus(next: StatusFilter): void {
  if (status.value === next) return
  status.value = next
  void load()
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="alerts-view">
    <header class="alerts-view__bar ip-panel">
      <div class="alerts-view__filters">
        <div class="alerts-view__seg" role="group" aria-label="告警状态筛选">
          <button
            v-for="tab in STATUS_TABS"
            :key="tab.value"
            class="alerts-view__seg-item"
            :class="{ 'is-active': status === tab.value }"
            type="button"
            :aria-pressed="status === tab.value"
            @click="switchStatus(tab.value)"
          >
            {{ tab.label }}
          </button>
        </div>

        <ElSelect
          v-model="level"
          class="alerts-view__level"
          size="small"
          aria-label="按级别筛选"
          @change="load"
        >
          <ElOption
            v-for="item in LEVEL_OPTIONS"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </ElSelect>

        <span class="alerts-view__count ip-dim">
          共 {{ formatNumber(rows.length) }} 条 · 未确认 {{ formatNumber(unackedCount) }} 条
        </span>
      </div>

      <div class="alerts-view__actions">
        <ElButton size="small" :loading="loading" @click="load">刷新</ElButton>
        <ElButton size="small" :disabled="unackedCount === 0" :loading="ackingAll" @click="onAckAll">
          全部确认
        </ElButton>
        <ElButton size="small" text type="primary" @click="toggleRules">
          {{ showRules ? '收起规则' : `查看规则（${ruleCount || '…'}）` }}
        </ElButton>
      </div>
    </header>

    <ErrorBanner :message="error" title="告警加载失败" @close="error = null" />

    <!-- 规则说明：只读回显，解释"为什么会报警" -->
    <section v-if="showRules" class="alerts-view__rules ip-panel">
      <header class="alerts-view__rules-head">
        <h2 class="ip-section-title">当前生效的规则</h2>
        <span class="ip-dim">
          {{ ruleCount }} 条 · 外部推送{{ webhookOn ? '已开启' : '未开启' }}
        </span>
      </header>
      <p class="alerts-view__rules-note">
        规则定义在 <code>backend/app/data/alert_rules.yaml</code>，每次任务完成后评估一次。
        阈值属于生产参数，因此<b>只在这里查看、不在网页上修改</b> —— 放在文件里可审计、可版本对比。
        文案里的 <code>{value}</code> 会在实际报警时替换为实测值。
      </p>
      <ul v-if="rules && rules.rules.length" class="alerts-view__rule-list">
        <li v-for="(rule, index) in rules.rules" :key="index" class="alerts-view__rule">
          <span class="alerts-view__rule-name">{{ rule.name ?? '未命名规则' }}</span>
          <ElTag :type="levelTone(rule.level ?? 'info')" size="small" effect="plain">
            {{ levelLabel(rule.level ?? 'info') }}
          </ElTag>
          <span class="alerts-view__rule-cond num">
            {{ rule.metric ?? 'unique_objects' }} · {{ rule.window ?? '本次任务' }} ≥
            {{ formatNumber(rule.threshold ?? 0) }}
          </span>
          <span v-if="rule.class_name" class="alerts-view__rule-extra">仅 {{ rule.class_name }}</span>
          <span class="alerts-view__rule-msg ip-dim">{{ rule.message ?? '（自动生成文案）' }}</span>
        </li>
      </ul>
      <p v-else-if="rulesLoading" class="ip-dim">正在读取规则…</p>
      <p v-else class="ip-dim">没有配置任何规则（或规则文件读取失败）。</p>
    </section>

    <section class="alerts-view__list ip-panel">
      <ul v-if="rows.length" class="alerts-view__items">
        <li
          v-for="row in rows"
          :key="row.id"
          class="alerts-view__item"
          :class="{ 'is-acked': row.acked, [`is-${row.level}`]: true }"
        >
          <div class="alerts-view__item-main">
            <div class="alerts-view__item-head">
              <ElTag :type="levelTone(row.level)" size="small" effect="dark">
                {{ levelLabel(row.level) }}
              </ElTag>
              <span class="alerts-view__item-message">{{ row.message }}</span>
            </div>
            <div class="alerts-view__item-meta">
              <span class="ip-label">规则</span>
              <span>{{ row.rule ?? '—' }}</span>
              <span v-if="row.value !== null" class="num">实测 {{ formatNumber(row.value) }}</span>
              <span class="num">{{ formatDateTime(row.ts) }}</span>
              <router-link
                v-if="row.task_id"
                class="alerts-view__link"
                :to="{ name: 'history', query: { task: String(row.task_id) } }"
              >
                查看任务 {{ formatTaskNo(row.task_id) }}
              </router-link>
            </div>
          </div>

          <div class="alerts-view__item-actions">
            <span v-if="row.acked" class="alerts-view__acked">
              <span class="ip-dot ip-dot--off" aria-hidden="true" />已确认
            </span>
            <ElButton
              v-else
              size="small"
              type="primary"
              plain
              :loading="ackingId === row.id"
              @click="onAck(row)"
            >
              确认
            </ElButton>
          </div>
        </li>
      </ul>

      <EmptyState
        v-else-if="!loading"
        description="没有符合条件的告警"
        :hint="
          status === 'unacked'
            ? '未确认的告警已处理完；也可以切到「全部」查看历史告警'
            : '系统会在每次检测任务完成后按 alert_rules.yaml 评估规则，命中才会产生告警'
        "
      />
      <p v-else class="alerts-view__loading ip-dim">正在加载告警…</p>
    </section>
  </div>
</template>

<style scoped lang="scss">
.alerts-view {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  min-height: 0;
  overflow-y: auto;

  &__bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ip-gap);
    flex-wrap: wrap;
    padding: 10px 12px;
  }

  &__filters,
  &__actions {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  &__seg {
    display: inline-flex;
    padding: 2px;
    border: 1px solid var(--ip-line);
    border-radius: 999px;
    background: var(--ip-panel-2);
  }

  &__seg-item {
    padding: 4px 14px;
    border: none;
    border-radius: 999px;
    background: transparent;
    color: var(--ip-text-2);
    font-size: var(--ip-fs-xs);
    cursor: pointer;

    &.is-active {
      background: var(--ip-primary-weak);
      color: var(--ip-primary-hover);
      font-weight: 600;
    }
  }

  &__level {
    width: 118px;
  }

  &__rules {
    padding: var(--ip-gap);
  }

  &__rules-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--ip-gap);
  }

  &__rules-note {
    margin: 6px 0 10px;
    font-size: var(--ip-fs-xs);
    line-height: 1.6;
    color: var(--ip-text-3);

    code {
      padding: 1px 5px;
      border-radius: 4px;
      background: var(--ip-panel-2);
      font-family: var(--ip-font-mono);
    }
  }

  &__rule-list {
    display: grid;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  &__rule {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    padding: 7px 10px;
    border: 1px solid var(--ip-line-soft);
    border-radius: var(--ip-radius-sm);
    background: var(--ip-panel-2);
    font-size: var(--ip-fs-xs);
  }

  &__rule-name {
    font-weight: 600;
    color: var(--ip-text);
  }

  &__rule-cond {
    color: var(--ip-text-2);
  }

  &__rule-extra {
    color: var(--ip-primary-hover);
  }

  &__rule-msg {
    flex: 1 1 200px;
    min-width: 0;
  }

  &__list {
    padding: 0;
  }

  &__items {
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  &__item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ip-gap);
    padding: 12px var(--ip-gap);
    border-bottom: 1px solid var(--ip-line-soft);
    /* 左侧色条按级别区分，扫一眼就知道轻重 */
    box-shadow: inset 3px 0 0 0 var(--ip-line-strong);

    &:last-child {
      border-bottom: none;
    }

    &.is-critical {
      box-shadow: inset 3px 0 0 0 var(--ip-danger);
    }

    &.is-warn {
      box-shadow: inset 3px 0 0 0 var(--ip-warn);
    }

    &.is-info {
      box-shadow: inset 3px 0 0 0 var(--ip-info);
    }

    &.is-acked {
      opacity: 0.62;
    }
  }

  &__item-main {
    display: grid;
    gap: 6px;
    min-width: 0;
  }

  &__item-head {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  &__item-message {
    font-size: var(--ip-fs-body);
    font-weight: 600;
    color: var(--ip-text);
  }

  &__item-meta {
    display: flex;
    align-items: baseline;
    gap: 12px;
    flex-wrap: wrap;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__link {
    color: var(--ip-primary-hover);
  }

  &__item-actions {
    flex: 0 0 auto;
  }

  &__acked {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__loading {
    padding: 24px 0;
    text-align: center;
  }
}
</style>
