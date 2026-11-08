<script setup lang="ts">
import { computed } from 'vue'
import { sortedCountEntries } from '@/utils'
import type { DetectMode } from '@/stores/session'
import type { CountMap } from '@/types'

const props = withDefaults(
  defineProps<{
    /** 本帧各类数量 */
    frameCounts: CountMap
    /** 累计框数 */
    totalObjects: number
    /** 去重虫数（需要突出显示） */
    uniqueObjects: number
    inferenceMs: number | null
    frameTotal: number
    frameIndex?: number
    activeTracks?: number
    elapsedSeconds?: number
    loading?: boolean
    live?: boolean
    /** 当前检测模式 —— 决定数字的口径（见下 tracked） */
    mode?: DetectMode
  }>(),
  {
    frameIndex: 0,
    activeTracks: 0,
    elapsedSeconds: 0,
    loading: false,
    live: false,
    mode: 'image'
  }
)

/**
 * 是否存在跨帧跟踪。
 *
 * 只有摄像头与视频有轨迹（`backend/app/services/tracker.py`），此时「去重虫数」
 * 才会真的少于「框数」。单图与批量是**每张图各自独立**的 —— 后端在
 * `routers/detection.py` 里明写 `task.unique_objects = len(detections)`（单图）与
 * `= total_objects`（批量），去重数与框数**恒等**。那种情况下再并排摆两块数字
 * 一样的磁贴、下面还写着"按轨迹去重后的独立目标"，就是在骗人。
 */
const tracked = computed(() => props.mode === 'camera' || props.mode === 'video')

/**
 * 摄像头模式下，去重虫数 / 累计框数 / 活动轨迹 / 运行时长这四项由左侧
 * `CameraPanel` 的磁贴负责（`DetectView.vue` 里这两个面板是同一屏并排的）。
 * 本面板再摆一遍就是同屏两份一模一样的数字，所以摄像头模式下只留
 * CameraPanel 没有的口径：本帧目标、已处理帧、推理耗时、本帧类别分布。
 *
 * 视频模式没有 CameraPanel 陪着，不受这条影响。
 */
const cameraPanelOwns = computed(() => props.mode === 'camera')

const entries = computed(() => sortedCountEntries(props.frameCounts))
const frameSum = computed(() => entries.value.reduce((acc, entry) => acc + entry[1], 0))
const maxCount = computed(() => entries.value.reduce((acc, entry) => Math.max(acc, entry[1]), 0))

interface HeroTile {
  label: string
  value: number
  hint: string
  primary: boolean
}

/**
 * 顶部两块大磁贴的内容。没有跨帧跟踪时**只留"框数"这一个口径**：
 * 批量的两块分别是"整批"与"当前这一张"（这两个数确实不同），单图则只有一块。
 * 摄像头模式返回空数组（磁贴由 CameraPanel 出），模板据此整块不渲染。
 */
const heroTiles = computed<HeroTile[]>(() => {
  // 摄像头模式：这两块数与 CameraPanel 顶部的磁贴重合，见 cameraPanelOwns
  if (cameraPanelOwns.value) return []
  if (tracked.value) {
    return [
      { label: '去重虫数', value: props.uniqueObjects, hint: '按轨迹去重后的独立目标', primary: true },
      { label: '累计框数', value: props.totalObjects, hint: '所有帧的检测框总和', primary: false }
    ]
  }
  if (props.mode === 'batch') {
    return [
      { label: '本批框数', value: props.totalObjects, hint: '整个批次所有图的检测框总和', primary: true },
      { label: '本张框数', value: props.frameTotal, hint: '当前这一张图的检测框', primary: false }
    ]
  }
  return [{ label: '本图框数', value: props.totalObjects, hint: '这张图的全部检测框', primary: true }]
})

function barWidth(count: number): string {
  if (maxCount.value <= 0) return '0%'
  return `${Math.max(4, Math.round((count / maxCount.value) * 100))}%`
}

const elapsedText = computed(() => {
  const total = Math.max(0, Math.floor(props.elapsedSeconds))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(minutes)}:${pad(seconds)}`
})
</script>

<template>
  <section class="stats ip-panel" aria-label="实时统计">
    <header class="stats__head">
      <h2 class="ip-section-title">实时统计</h2>
      <span v-if="live" class="stats__badge">
        <span class="ip-dot ip-dot--ok" aria-hidden="true" />
        LIVE
      </span>
    </header>

    <div
      v-if="heroTiles.length > 0"
      class="stats__hero"
      :class="{ 'stats__hero--single': heroTiles.length === 1 }"
    >
      <div
        v-for="tile in heroTiles"
        :key="tile.label"
        class="stats__hero-item"
        :class="{ 'stats__hero-item--primary': tile.primary }"
      >
        <span class="stats__hero-label">{{ tile.label }}</span>
        <span class="stats__hero-value num">{{ tile.value }}</span>
        <span class="stats__hero-hint">{{ tile.hint }}</span>
      </div>
    </div>

    <dl class="stats__grid">
      <!-- 本帧目标/已处理帧/活动轨迹都依赖跨帧跟踪，单图/批量下要么恒等、要么恒为 0，
           所以不摆（只有推理耗时是任何模式都有意义的）。活动轨迹在摄像头模式下也省掉
           —— CameraPanel 里已经有一块同样的数，见 cameraPanelOwns。 -->
      <div v-if="tracked">
        <dt>本帧目标</dt>
        <dd class="num">{{ frameSum }}</dd>
      </div>
      <div v-if="tracked">
        <dt>已处理帧</dt>
        <dd class="num">{{ frameTotal }}</dd>
      </div>
      <div>
        <dt>推理耗时</dt>
        <dd class="num">{{ inferenceMs === null ? '—' : `${inferenceMs.toFixed(0)} ms` }}</dd>
      </div>
      <div v-if="tracked && !cameraPanelOwns">
        <dt>活动轨迹</dt>
        <dd class="num">{{ activeTracks }}</dd>
      </div>
    </dl>

    <div v-if="live && !cameraPanelOwns" class="stats__elapsed">
      <span class="ip-label">累计运行</span>
      <span class="num">{{ elapsedText }}</span>
    </div>
    <div class="stats__list">
      <p class="stats__list-title">本帧各类数量</p>
      <p v-if="entries.length === 0" class="stats__empty">
        {{ loading ? '正在检测…' : '本帧暂无检出目标' }}
      </p>
      <ul v-else class="stats__rows">
        <li v-for="entry in entries" :key="entry[0]" class="stats__row">
          <span class="stats__row-name ip-truncate">{{ entry[0] }}</span>
          <span class="stats__row-bar" aria-hidden="true">
            <span class="stats__row-bar-fill" :style="{ width: barWidth(entry[1]) }" />
          </span>
          <span class="stats__row-count num">{{ entry[1] }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<style scoped lang="scss">
.stats {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-family: var(--ip-font-mono);
    font-size: var(--ip-fs-xs);
    letter-spacing: 0.08em;
    color: var(--ip-primary-hover);
  }

  &__hero {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }

  /* 只有一个口径可讲时（单图模式），让磁贴占满整行 */
  &__hero--single {
    grid-template-columns: 1fr;
  }

  &__hero-item {
    display: flex;
    flex-direction: column;
    padding: 8px 10px;
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
    background: var(--ip-panel-2);
  }

  &__hero-item--primary {
    border-color: var(--ip-primary-line);
    background: var(--ip-primary-weak);
  }

  &__hero-label {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__hero-value {
    font-size: 26px;
    font-weight: 600;
    line-height: 1.15;
    color: var(--ip-text);
  }

  &__hero-item--primary .stats__hero-value {
    color: var(--ip-primary-hover);
  }

  &__hero-hint {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px 10px;
    margin: 0;

    dt {
      font-size: var(--ip-fs-xs);
      color: var(--ip-text-3);
    }

    dd {
      margin: 0;
      font-size: var(--ip-fs-body);
      color: var(--ip-text);
    }
  }

  &__elapsed {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 8px;
    border-radius: var(--ip-radius-sm);
    background: var(--ip-panel-2);
    font-size: var(--ip-fs-xs);
  }

  &__list {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding-top: 8px;
    border-top: 1px solid var(--ip-line-soft);
  }

  &__list-title {
    font-size: var(--ip-fs-xs);
    letter-spacing: 0.04em;
    color: var(--ip-text-3);
  }

  &__empty {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    padding: 4px 0;
  }

  &__rows {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 210px;
    overflow-y: auto;
  }

  &__row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 54px 28px;
    align-items: center;
    gap: 8px;
    font-size: var(--ip-fs-xs);
  }

  &__row-name {
    color: var(--ip-text-2);
  }

  &__row-bar {
    display: block;
    height: 4px;
    border-radius: 2px;
    background: var(--ip-panel-3);
    overflow: hidden;
  }

  &__row-bar-fill {
    display: block;
    height: 100%;
    background: var(--ip-primary);
    border-radius: 2px;
  }

  &__row-count {
    text-align: right;
    color: var(--ip-text);
  }
}
</style>
