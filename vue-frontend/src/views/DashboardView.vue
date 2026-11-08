<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton, ElTooltip } from 'element-plus'
import ClassPieChart from '@/components/charts/ClassPieChart.vue'
import HourBarChart from '@/components/charts/HourBarChart.vue'
import TrendLineChart from '@/components/charts/TrendLineChart.vue'
import AiReportDrawer from '@/components/ai/AiReportDrawer.vue'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import StatCard from '@/components/common/StatCard.vue'
import { STATS_RANGES, statsSummary } from '@/api/stats'
import { getTaskCounts, listTasks } from '@/api/tasks'
import { useModelsStore } from '@/stores/models'
import { formatDateTime, formatNumber, formatTaskNo } from '@/utils'
import { notifyApiError } from '@/utils/notify'
import type { StatsRange, StatsSummaryOut, TaskOut } from '@/types'

const router = useRouter()
const models = useModelsStore()

const range = ref<StatsRange>('7d')
const reportDrawerOpen = ref(false)
const summary = ref<StatsSummaryOut | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const speciesCount = computed(() => summary.value?.by_class?.length ?? 0)

const topClass = computed(() => {
  const list = summary.value?.by_class ?? []
  const first = list[0]
  if (!first) return null
  return list.reduce((acc, item) => (item.count > acc.count ? item : acc), first)
})

const avgPerTask = computed(() => {
  const data = summary.value
  if (!data || data.total_tasks === 0) return 0
  return data.unique_objects / data.total_tasks
})

const hasData = computed(() => {
  const data = summary.value
  if (!data) return false
  return data.total_tasks > 0 || data.total_objects > 0
})

const rangeLabel = computed(
  () => STATS_RANGES.find((item) => item.value === range.value)?.label ?? range.value
)

const plotHint = computed(
  () =>
    `按任务的 plot_id 去重（当前 ${formatNumber(summary.value?.plot_count ?? 0)} 个地块）。` +
    '后端已提供 /api/plots 管理接口，前端尚未提供界面。'
)

/**
 * 最近检出图片墙。
 *
 * 这是看板上唯一"看得见虫子"的地方：统计数字与图表能说明趋势，
 * 但用户打开系统最想先确认的是"最近拍到了什么"。只取有标注图的任务。
 *
 * 分类计数需要逐任务取（列表接口不带 counts），因此并发请求、失败即降级
 * —— 图片仍要显示，不能因为取名失败就整体空掉。
 */
const RECENT_LIMIT = 6
const recent = ref<Array<TaskOut & { topClass: string | null }>>([])
const recentLoading = ref(false)

async function loadRecent(): Promise<void> {
  recentLoading.value = true
  try {
    const page = await listTasks({ page: 1, page_size: RECENT_LIMIT * 2 })
    const withImage = page.items.filter((item) => item.result_image_path).slice(0, RECENT_LIMIT)
    const counts = await Promise.all(withImage.map((item) => getTaskCounts(item.id).catch(() => null)))
    recent.value = withImage.map((item, index) => {
      const detail = counts[index]
      const entries = detail ? Object.entries(detail.counts).sort((a, b) => b[1] - a[1]) : []
      const first = entries[0]
      return { ...item, topClass: first ? `${first[0]} ×${first[1]}` : null }
    })
  } catch {
    recent.value = []
  } finally {
    recentLoading.value = false
  }
}

async function goHistory(): Promise<void> {
  await router.push({ name: 'history' })
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    summary.value = await statsSummary(range.value)
  } catch (err) {
    summary.value = null
    error.value = notifyApiError(err)
  } finally {
    loading.value = false
  }
}

async function switchRange(next: StatsRange): Promise<void> {
  if (range.value === next) return
  range.value = next
  await load()
}

async function goDetect(): Promise<void> {
  await router.push({ name: 'detect' })
}

onMounted(() => {
  void load()
  void loadRecent()
  void models.loadRegistry()
})
</script>

<template>
  <div class="dashboard">
    <header class="dashboard__bar ip-panel">
      <div class="dashboard__range" role="group" aria-label="统计时间范围">
        <button
          v-for="item in STATS_RANGES"
          :key="item.value"
          class="dashboard__range-item"
          :class="{ 'is-active': range === item.value }"
          type="button"
          :aria-label="`切换到${item.label}`"
          :aria-pressed="range === item.value"
          @click="switchRange(item.value)"
        >
          {{ item.label }}
        </button>
      </div>
      <div class="dashboard__bar-right">
        <span class="dashboard__range-note">
          统计范围：{{ rangeLabel }} · 当前模型 {{ models.currentName }}
        </span>
        <ElButton size="small" :loading="loading" aria-label="刷新统计数据" @click="load">
          刷新
        </ElButton>
        <!-- 数据看完顺手出报告：同一段工作流放在同一个界面 -->
        <ElButton
          size="small"
          type="primary"
          aria-label="生成 AI 诊断报告"
          @click="reportDrawerOpen = true"
        >
          AI 诊断报告
        </ElButton>
      </div>
    </header>

    <AiReportDrawer v-model="reportDrawerOpen" />

    <ErrorBanner :message="error" title="统计加载失败" @close="error = null" />

    <section class="dashboard__cards">
      <StatCard
        label="任务数"
        :value="summary?.total_tasks ?? 0"
        :loading="loading && !summary"
        hint="该范围内已登记的检测任务"
      />
      <StatCard
        label="去重虫数"
        :value="summary?.unique_objects ?? 0"
        emphasis
        :loading="loading && !summary"
        hint="按轨迹去重后的独立目标"
      />
      <StatCard
        label="框总数"
        :value="summary?.total_objects ?? 0"
        :loading="loading && !summary"
        hint="所有帧的检测框累加"
      />
      <StatCard
        label="虫种数"
        :value="speciesCount"
        :loading="loading && !summary"
        :hint="topClass ? `最多：${topClass.zh_name} ${topClass.count}` : '该范围内未检出虫种'"
      />
      <StatCard
        label="平均每任务虫数"
        :value="Number(avgPerTask.toFixed(1))"
        :digits="1"
        tone="info"
        :loading="loading && !summary"
        hint="去重虫数 ÷ 任务数"
      />
      <ElTooltip :content="plotHint" placement="top">
        <div class="dashboard__plot-card">
          <StatCard
            label="地块数"
            :value="formatNumber(summary?.plot_count ?? 0)"
            :loading="loading && !summary"
            hint="按任务的 plot_id 去重"
          />
        </div>
      </ElTooltip>
    </section>

    <EmptyState
      v-if="!hasData && !loading && !error"
      description="所选时间范围内还没有统计数据"
      hint="到检测台跑一次检测后，这里会显示类别分布、按天趋势与 24 小时分布"
      action-text="前往检测台"
      @action="goDetect"
    />

    <template v-else>
      <!-- 最近检出：看板上唯一"看得见虫子"的地方。
           统计与图表说明趋势，但用户打开系统最想先确认"最近拍到了什么"。 -->
      <section class="dashboard__recent ip-panel">
        <header class="dashboard__recent-head">
          <div>
            <h3 class="ip-section-title">最近检出</h3>
            <p class="ip-page-sub">最新带标注结果的检测，点击进入历史查看明细</p>
          </div>
          <ElButton text size="small" type="primary" @click="goHistory">查看全部历史</ElButton>
        </header>

        <ul v-if="recent.length" class="dashboard__gallery">
          <li v-for="item in recent" :key="item.id">
            <button
              class="dashboard__shot ip-panel--action"
              type="button"
              :aria-label="`查看 ${formatTaskNo(item.id)} 的检测详情`"
              @click="goHistory"
            >
              <span class="ip-thumb dashboard__shot-img">
                <img
                  :src="item.result_image_path ?? ''"
                  :alt="`任务 ${formatTaskNo(item.id)} 的标注结果`"
                  loading="lazy"
                />
              </span>
              <span class="dashboard__shot-meta">
                <span class="dashboard__shot-id num">{{ formatTaskNo(item.id) }}</span>
                <span class="dashboard__shot-pest ip-truncate">{{ item.topClass ?? '未检出目标' }}</span>
                <span class="dashboard__shot-time num">{{ formatDateTime(item.started_at) }}</span>
              </span>
            </button>
          </li>
        </ul>
        <p v-else class="dashboard__recent-empty ip-dim">
          {{ recentLoading ? '正在加载最近检出…' : '还没有带标注图的检测结果' }}
        </p>
      </section>

      <section class="dashboard__row">
        <ClassPieChart
          class="dashboard__pie"
          :items="summary?.by_class ?? []"
          :loading="loading && !summary"
        />
        <TrendLineChart
          class="dashboard__line"
          :items="summary?.by_day ?? []"
          :loading="loading && !summary"
        />
      </section>

      <section class="dashboard__row dashboard__row--full">
        <HourBarChart
          class="dashboard__hours"
          :hours="summary?.by_hour ?? []"
          :loading="loading && !summary"
        />
        <div class="dashboard__notes ip-panel">
          <h3 class="ip-section-title">指标口径</h3>
          <ul class="dashboard__note-list">
            <li>
              <span class="ip-label">数据来源</span>
              <span>GET /api/stats/summary?range={{ range }}（服务端聚合）</span>
            </li>
            <li>
              <span class="ip-label">框总数 vs 去重虫数</span>
              <span>前者是每帧检测框累加，后者按 track_id 去重，更接近真实虫口数</span>
            </li>
            <li>
              <span class="ip-label">图表实现</span>
              <span>手写内联 SVG，主题切换时随 CSS 变量自动换色</span>
            </li>
            <li>
              <span class="ip-label">地块</span>
              <span>按任务的 plot_id 去重；后端已提供 /api/plots 接口，前端界面待补</span>
            </li>
          </ul>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped lang="scss">
.dashboard {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;

  &__bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 10px;
  }

  &__range {
    display: inline-flex;
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
    overflow: hidden;
  }

  &__range-item {
    padding: 4px 12px;
    font-size: var(--ip-fs-xs);
    background: transparent;
    border: none;
    color: var(--ip-text-2);
    cursor: pointer;
  }

  &__range-item.is-active {
    background: var(--ip-primary-weak);
    color: var(--ip-text);
  }

  &__bar-right {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  &__range-note {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
    gap: 10px;
  }

  &__device-card {
    display: block;
    cursor: help;
  }

  &__row {
    display: grid;
    grid-template-columns: minmax(0, 380px) minmax(0, 1fr);
    gap: var(--ip-gap);
    align-items: start;
  }

  &__row--full {
    grid-template-columns: minmax(0, 1fr) minmax(0, 320px);
  }

  &__notes {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
  }

  &__note-list {
    display: flex;
    flex-direction: column;
    gap: 8px;

    li {
      display: flex;
      flex-direction: column;
      gap: 1px;
      font-size: var(--ip-fs-xs);
      line-height: 1.55;
      color: var(--ip-text-2);
    }
  }
}

@media (max-width: 1279px) {
  .dashboard__row,
  .dashboard__row--full {
    grid-template-columns: minmax(0, 1fr);
  }
}
/* ---------------- 最近检出图片墙 ---------------- */
.dashboard__recent {
  padding: var(--ip-gap);

  &-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--ip-gap);
    margin-bottom: 10px;
  }

  &-empty {
    padding: 16px 0;
    text-align: center;
    font-size: var(--ip-fs-sm);
  }
}

.dashboard__gallery {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.dashboard__shot {
  display: block;
  width: 100%;
  padding: 0;
  border: 1px solid var(--ip-line);
  border-radius: var(--ip-radius);
  background: var(--ip-panel-2);
  color: inherit;
  text-align: left;
  cursor: pointer;
  overflow: hidden;

  &-img {
    display: block;
    width: 100%;
    aspect-ratio: 4 / 3;
    border: none;
    border-radius: 0;
  }

  &-meta {
    display: grid;
    gap: 2px;
    padding: 7px 9px 9px;
  }

  &-id {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &-pest {
    font-size: var(--ip-fs-body);
    font-weight: 600;
    color: var(--ip-text);
  }

  &-time {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }
}
</style>
