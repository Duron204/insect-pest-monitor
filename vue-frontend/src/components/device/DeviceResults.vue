<script setup lang="ts">
/**
 * 一台盒子的检测结果：概览数字 + 趋势 + 最近一次上报的框明细 + 最近结果缩略图。
 *
 * 三条设计取舍：
 *   1. **不自己起定时器**。父级（详情页）本来就在轮询这台设备；这里监听
 *      `device.last_task_at` 变化再重拉结果 —— 用同一个信号，避免两个定时器
 *      各刷各的、还可能刷出不一致的画面。
 *   2. **趋势复用 `statsSummary` 的 `deviceId` 过滤**，不新增聚合接口。
 *      后端会先校验归属（不是我的 → 404），所以这个参数不是旁路。
 *   3. **点结果不另写抽屉**：跳到历史页并带上 `?task=`，复用现成的
 *      `TaskDetailDrawer`。那里已经有框明细、报告导出、标注图对比。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton, ElSelect, ElOption } from 'element-plus'
import StatCard from '@/components/common/StatCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import TrendLineChart from '@/components/charts/TrendLineChart.vue'
import DetectionTable from '@/components/detection/DetectionTable.vue'
import { STATS_RANGES, statsSummary } from '@/api/stats'
import { listDeviceTasks } from '@/api/devices'
import { listTaskDetections } from '@/api/tasks'
import {
  formatNumber,
  formatRelative,
  formatTaskNo,
  taskStatusLabel
} from '@/utils'
import { notifyApiError } from '@/utils/notify'
import type { Detection, DetectionRow, DeviceOut, StatsRange, StatsSummaryOut, TaskOut } from '@/types'

const props = defineProps<{ device: DeviceOut }>()

const router = useRouter()

const range = ref<StatsRange>('30d')
const summary = ref<StatsSummaryOut | null>(null)
const recent = ref<TaskOut[]>([])
const latestRows = ref<DetectionRow[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const RECENT_LIMIT = 8
const DETECTION_LIMIT = 200

const hasResults = computed(() => recent.value.length > 0)
const latestTask = computed<TaskOut | null>(() => recent.value[0] ?? null)

function toRows(taskId: number, detections: Detection[]): DetectionRow[] {
  return detections.map((item, index) => ({
    // 同一个 key 只要在一个表里唯一即可；带上 taskId 是为了"最近一次"换任务时
    // 表格能整块重绘，不会把上一条任务的选中态错配到新行上
    key: `${taskId}-${index}`,
    frameLabel: formatTaskNo(taskId),
    frameIndex: 1,
    classId: item.class_id,
    className: item.class_name,
    zhName: item.zh_name,
    score: item.score,
    bbox: item.bbox,
    trackId: item.track_id
  }))
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [stats, page] = await Promise.all([
      statsSummary(range.value, { deviceId: props.device.id }),
      listDeviceTasks(props.device.id, { page: 1, page_size: RECENT_LIMIT })
    ])
    summary.value = stats
    recent.value = page.items
    error.value = null

    const first = page.items[0]
    if (first) {
      const detections = await listTaskDetections(first.id, { page: 1, page_size: DETECTION_LIMIT })
      latestRows.value = toRows(first.id, detections.items)
    } else {
      latestRows.value = []
    }
  } catch (err) {
    error.value = notifyApiError(err)
  } finally {
    loading.value = false
  }
}

function openTask(id: number): void {
  void router.push({ name: 'history', query: { task: String(id) } })
}

onMounted(load)

// 有新上报就重拉；range 变了也重拉
watch(() => props.device.last_task_at, () => void load())
watch(range, () => void load())
</script>

<template>
  <section class="results">
    <header class="results__head">
      <div>
        <h3 class="ip-section-title">检测结果</h3>
        <p class="ip-page-sub">
          盒子按间隔定时抓拍、在本机推理，把<strong>原图 + 框坐标</strong>报给中心，
          标注图由中心画 —— 所以这里的每一张都是中心收到之后自己烧的框。
        </p>
      </div>
      <ElSelect v-model="range" size="small" class="results__range" aria-label="统计区间">
        <ElOption
          v-for="item in STATS_RANGES"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </ElSelect>
    </header>

    <p v-if="error" class="results__error">{{ error }}</p>

    <div class="results__cards">
      <StatCard
        label="检出任务"
        :value="summary?.total_tasks ?? null"
        :loading="loading && !summary"
        hint="区间内这台盒子报上来的抓拍次数"
      />
      <StatCard
        label="去重虫数"
        :value="summary?.unique_objects ?? null"
        emphasis
        :loading="loading && !summary"
        hint="按轨迹去重，比框总数更接近真实虫口"
      />
      <StatCard
        label="框总数"
        :value="summary?.total_objects ?? null"
        :loading="loading && !summary"
        hint="所有帧里的检测框累加"
      />
      <StatCard
        label="最近上报"
        :value="device.last_task_at ? formatRelative(device.last_task_at) : '还没上报过'"
        :loading="loading && !summary"
        hint="盒子本机推理完成后立即上报"
      />
    </div>

    <div class="results__trend ip-panel">
      <h4 class="results__sub">按天趋势</h4>
      <TrendLineChart :items="summary?.by_day ?? []" :loading="loading && !summary" />
    </div>

    <div class="results__recent ip-panel">
      <header class="results__recent-head">
        <div>
          <h4 class="results__sub">最近结果</h4>
          <p class="ip-page-sub">点开任意一张，在历史页里看标注图对比、框明细与报告导出。</p>
        </div>
        <!-- 只跳历史页，不带 ?mode=device：HistoryView 目前只认 ?task=，
             塞一个它不读的参数会让人以为筛过了、其实没有。
             想只看这台盒子的结果，用历史页的模式筛选（已加「设备上报」）。 -->
        <ElButton text size="small" type="primary" @click="router.push({ name: 'history' })">
          查看全部
        </ElButton>
      </header>

      <ul v-if="hasResults" class="results__gallery">
        <li v-for="item in recent" :key="item.id">
          <button
            class="results__shot"
            type="button"
            :aria-label="`查看 ${formatTaskNo(item.id)} 的检测详情`"
            @click="openTask(item.id)"
          >
            <span class="ip-thumb results__shot-img">
              <img
                v-if="item.result_image_path"
                :src="item.result_image_path"
                :alt="`任务 ${formatTaskNo(item.id)} 的标注结果`"
                loading="lazy"
              />
              <span v-else class="results__shot-noimg">无标注图</span>
            </span>
            <span class="results__shot-meta">
              <span class="results__shot-id num">{{ formatTaskNo(item.id) }}</span>
              <span class="results__shot-counts num">
                去重 {{ formatNumber(item.unique_objects) }}
                <span class="results__shot-sep">/</span>
                <span class="ip-dim">框 {{ formatNumber(item.total_objects) }}</span>
              </span>
              <span class="results__shot-time num ip-dim">
                {{ formatRelative(item.started_at) }} · {{ taskStatusLabel(item.status) }}
              </span>
            </span>
          </button>
        </li>
      </ul>

      <EmptyState
        v-else
        compact
        :description="loading ? '正在加载检测结果…' : '这台盒子还没有上报过结果'"
        hint="盒子绑定后会按抓拍间隔（默认与 local_camera.interval_min 一致）自动上报，第一张结果需要等一个间隔"
      />
    </div>

    <div v-if="latestTask" class="results__boxes ip-panel">
      <header class="results__recent-head">
        <div>
          <h4 class="results__sub">
            最近一次上报的框明细
            <span class="ip-dim num">{{ formatTaskNo(latestTask.id) }}</span>
          </h4>
          <p class="ip-page-sub">
            单帧抓拍，所以没有「画面」这一列 —— 一条任务就是一张图。
          </p>
        </div>
      </header>
      <DetectionTable
        :rows="latestRows"
        :selected-key="null"
        :hovered-key="null"
        :show-frame-column="false"
        :loading="loading"
        empty-text="这一次上报里没有检测到目标"
      />
    </div>
  </section>
</template>

<style scoped lang="scss">
.results {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--ip-gap);
  }

  &__range {
    flex: 0 0 auto;
    width: 130px;
  }

  &__sub {
    margin: 0;
    font-size: var(--ip-fs-sm);
    font-weight: 600;
    color: var(--ip-text);
  }

  &__error {
    margin: 0;
    font-size: var(--ip-fs-sm);
    color: var(--ip-danger);
  }

  &__cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
    gap: 10px;
  }

  &__trend,
  &__recent,
  &__boxes {
    padding: var(--ip-gap);
  }

  &__recent-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--ip-gap);
    margin-bottom: 10px;
  }

  &__gallery {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  &__shot {
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

    &:hover {
      border-color: var(--ip-line-strong);
    }

    &-img {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      aspect-ratio: 4 / 3;
      border: none;
      border-radius: 0;
    }

    &-noimg {
      font-size: var(--ip-fs-xs);
      color: var(--ip-text-3);
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

    &-counts {
      display: flex;
      align-items: baseline;
      gap: 4px;
      font-size: var(--ip-fs-body);
      font-weight: 600;
      color: var(--ip-text);
    }

    &-sep {
      color: var(--ip-text-3);
      font-weight: 400;
    }

    &-time {
      font-size: var(--ip-fs-xs);
    }
  }
}
</style>
