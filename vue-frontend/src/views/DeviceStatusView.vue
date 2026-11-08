<script setup lang="ts">
/**
 * 设备状态 —— 「运行后端的这台机器还好吗」+「它最近拍到了什么」。
 *
 * 为什么单开一页：现场部署时后端就跑在树莓派上，而 Pi 5 **欠压/热降频会让推理耗时翻倍
 * 且极难排查**（deploy/README-pi.md 把它列为第一条最需要先确认的事）。这些指标此前
 * 只有在树莓派上敲 `vcgencmd measure_temp` / `df` 才看得到，网页上没有入口。
 *
 * 两条贯穿本页的约定：
 *   1. **`null` = 没测到，不是 0**（口径来自 deploy/field-soak.sh）。开发机上没有
 *      vcgencmd，温度/降频/重启次数都会是 null —— 这时显示「未测到」，绝不显示 0，
 *      否则会把一个测不到指标的盒子误判为健康。
 *   2. 本页讲的是**后端所在的那台设备**，不是打开网页这台电脑。现场两者常被混淆，
 *      所以非树莓派时会明说当前跑在哪。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton } from 'element-plus'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import StatCard from '@/components/common/StatCard.vue'
import { systemApi, tasksApi } from '@/api'
import { formatDateTime, formatTaskNo, taskStatusLabel } from '@/utils'
import { notifyApiError } from '@/utils/notify'
import {
  TEMP_WARN_C,
  diskFreeText,
  diskHint,
  hostLabel as describeHostLabel,
  isThrottledProblem,
  memFreeText,
  memHint,
  restartsText,
  tempText,
  tempTone,
  tempUnit,
  throttleDotTone,
  throttleText,
  uptimeText
} from '@/utils/hostMetrics'
import type { HostStatusOut, TaskOut } from '@/types'

const router = useRouter()

const status = ref<HostStatusOut | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)

const RECENT_LIMIT = 12
const recent = ref<TaskOut[]>([])
const recentLoading = ref(false)

/** 主机指标变化慢，且每次采集后端都要起 vcgencmd 子进程 —— 15 秒足够，比摄像头面板保守 */
const POLL_MS = 15_000
let pollTimer: number | null = null

const isPi = computed(() => status.value?.is_raspberry_pi === true)

/**
 * 格式化全部下沉到 `@/utils/hostMetrics`（纯函数，好测）。
 * 这里只保留"页面状态"本身，不再散落 `?? '未测到'` 之类的判断 ——
 * 那些判断一旦有第二处实现，迟早漏掉一个，把"没测到"显示成 0。
 */
const hostLabel = computed(() => describeHostLabel(status.value))
const headerTone = computed(() => `ip-dot--${throttleDotTone(status.value)}`)

/* 告警块用到的两个值 —— `v-if="isThrottledProblem(status)"` 是函数调用，
   模板里 TS 无法据此把 `status` 收窄成非空，故取成 computed。 */
const alarmRaw = computed(() => status.value?.throttled ?? '?')
const alarmFlags = computed(() => status.value?.throttled_flags ?? [])

async function loadStatus(): Promise<void> {
  try {
    status.value = await systemApi.hostStatus()
    error.value = null
  } catch (err) {
    error.value = notifyApiError(err)
  } finally {
    loading.value = false
  }
}

/**
 * 最近检出只取 `mode=local`（本机摄像头定时抓拍/手动抓拍）。
 * 这与统计看板的「最近检出」是两回事：那边混全部模式，这里回答的是
 * "**树莓派自己**拍到了什么"。
 */
async function loadRecent(): Promise<void> {
  recentLoading.value = true
  try {
    const page = await tasksApi.listTasks({ page: 1, page_size: RECENT_LIMIT, mode: 'local' })
    recent.value = page.items
  } catch {
    // 与告警红点同样的取舍：最近检出取不到不该让整页报错，主机指标才是主角
    recent.value = []
  } finally {
    recentLoading.value = false
  }
}

async function refresh(): Promise<void> {
  loading.value = true
  await Promise.all([loadStatus(), loadRecent()])
}

async function openTask(id: number): Promise<void> {
  await router.push({ name: 'history', query: { task: String(id) } })
}

async function goDetect(): Promise<void> {
  await router.push({ name: 'detect' })
}

onMounted(async () => {
  loading.value = true
  await refresh()
  pollTimer = window.setInterval(() => void loadStatus(), POLL_MS)
})

onBeforeUnmount(() => {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<template>
  <div class="device">
    <header class="device__bar ip-panel">
      <div class="device__identity">
        <span class="ip-dot" :class="headerTone" aria-hidden="true" />
        <div>
          <h2 class="ip-section-title">设备状态</h2>
          <p class="ip-page-sub">
            {{ isPi ? '运行后端的这台树莓派' : '运行后端的这台设备' }} ·
            <span class="num">{{ hostLabel }}</span>
            <template v-if="status?.sampled_at">
              · 采样于 <span class="num">{{ formatDateTime(status.sampled_at) }}</span>
            </template>
          </p>
        </div>
      </div>
      <ElButton size="small" :loading="loading" aria-label="刷新设备状态" @click="refresh">
        刷新
      </ElButton>
    </header>

    <ErrorBanner :message="error" title="设备状态加载失败" @close="error = null" />

    <!-- 非树莓派时明说"当前管的是哪台机器"：开发机上温度/降频天然测不到，
         不说清楚会让人以为系统坏了 -->
    <p v-if="status && !isPi" class="device__note ip-panel">
      当前后端运行在<strong>开发机</strong>（{{ status.platform }} · {{ status.machine }}），
      不是树莓派。温度、降频与重启次数是树莓派专属指标，此处显示「未测到」属正常现象；
      磁盘、内存与运行时长任何平台都能采集。现场部署到树莓派后，这一页显示的就是树莓派自己。
    </p>

    <!-- 降频/欠压警示：现场第一大坑，必须一眼可见 -->
    <div v-if="isThrottledProblem(status)" class="device__alarm" role="alert">
      <strong>检测到欠压或降频（throttled = {{ alarmRaw }}）</strong>
      <p class="device__alarm-flags">
        <span
          v-for="flag in alarmFlags"
          :key="flag"
          class="ip-chip ip-chip--danger"
        >{{ flag }}</span>
      </p>
      <p class="device__alarm-hint">
        先解决供电与散热（官方 27W 电源 + 主动散热），再看性能数据 ——
        否则推理耗时的测量结果没有参考价值。
      </p>
    </div>

    <section class="device__cards">
      <StatCard
        label="CPU 温度"
        :value="tempText(status)"
        :unit="tempUnit(status)"
        :tone="tempTone(status)"
        :loading="loading && !status"
        :hint="`超过 ${TEMP_WARN_C}°C 需要留意散热`"
      />
      <StatCard
        label="降频 / 欠压"
        :value="throttleText(status)"
        :tone="isThrottledProblem(status) ? 'warn' : 'default'"
        :loading="loading && !status"
        hint="get_throttled，0x0 为正常"
      />
      <StatCard
        label="磁盘剩余"
        :value="diskFreeText(status)"
        :loading="loading && !status"
        :hint="diskHint(status)"
      />
      <StatCard
        label="内存可用"
        :value="memFreeText(status)"
        :loading="loading && !status"
        :hint="memHint(status)"
      />
      <StatCard
        label="服务运行时长"
        :value="uptimeText(status)"
        :loading="loading && !status"
        hint="后端进程持续运行时间"
      />
      <StatCard
        label="重启次数"
        :value="restartsText(status)"
        :loading="loading && !status"
        hint="systemd 部署下才有此项"
      />
    </section>

    <section class="device__recent ip-panel">
      <header class="device__recent-head">
        <div>
          <h3 class="ip-section-title">最近本机抓拍</h3>
          <p class="ip-page-sub">
            树莓派自己定时/手动抓拍的检出结果。去重虫数按轨迹去重，比框总数更接近真实虫口。
          </p>
        </div>
        <ElButton text size="small" type="primary" @click="router.push({ name: 'history' })">
          查看全部历史
        </ElButton>
      </header>

      <ul v-if="recent.length" class="device__gallery">
        <li v-for="item in recent" :key="item.id">
          <button
            class="device__shot"
            type="button"
            :aria-label="`查看 ${formatTaskNo(item.id)} 的检测详情`"
            @click="openTask(item.id)"
          >
            <span class="ip-thumb device__shot-img">
              <img
                v-if="item.result_image_path"
                :src="item.result_image_path"
                :alt="`任务 ${formatTaskNo(item.id)} 的标注结果`"
                loading="lazy"
              />
              <span v-else class="device__shot-noimg">无标注图</span>
            </span>
            <span class="device__shot-meta">
              <span class="device__shot-id num">{{ formatTaskNo(item.id) }}</span>
              <span class="device__shot-counts">
                <span class="num">去重 {{ item.unique_objects }}</span>
                <span class="device__shot-sep">/</span>
                <span class="ip-dim num">框 {{ item.total_objects }}</span>
              </span>
              <span class="device__shot-time num">
                {{ formatDateTime(item.started_at) }} · {{ taskStatusLabel(item.status) }}
              </span>
            </span>
          </button>
        </li>
      </ul>

      <EmptyState
        v-else
        compact
        :description="recentLoading ? '正在加载最近抓拍…' : '还没有本机抓拍记录'"
        hint="树莓派定时抓拍或手动抓拍后，结果会出现在这里，并进入历史与报告"
        action-text="前往检测台"
        @action="goDetect"
      />
    </section>

    <p class="device__foot ip-dim">
      数据来源：GET /api/system/host（主机健康）与 GET /api/tasks?mode=local（本机抓拍）。
      主机指标口径与 deploy/field-soak.sh 的现场巡检 CSV 一致，便于两边对照。
    </p>
  </div>
</template>

<style scoped lang="scss">
.device {
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
    padding: 8px 12px;
  }

  &__identity {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  &__note {
    margin: 0;
    padding: 10px 12px;
    font-size: var(--ip-fs-sm);
    line-height: 1.7;
    color: var(--ip-text-2);
  }

  /* 欠压/降频用整块警示，而不是又一个卡片：它必须是页面上最先被看见的东西 */
  &__alarm {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 12px;
    border: 1px solid var(--ip-danger);
    border-left-width: 3px;
    border-radius: var(--ip-radius);
    background: var(--ip-danger-weak);
    font-size: var(--ip-fs-sm);
    color: var(--ip-text);

    &-flags {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0;
    }

    &-hint {
      margin: 0;
      color: var(--ip-text-2);
      line-height: 1.6;
    }
  }

  &__cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
    gap: 10px;
  }

  &__recent {
    padding: var(--ip-gap);

    &-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: var(--ip-gap);
      margin-bottom: 10px;
    }
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
      color: var(--ip-text-3);
    }
  }

  &__foot {
    margin: 0;
    font-size: var(--ip-fs-xs);
    line-height: 1.6;
  }
}
</style>
