<script setup lang="ts">
import { computed, ref } from 'vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { useChartTheme } from '@/composables/useChartTheme'
import type { DayCountOut } from '@/types'

const props = withDefaults(
  defineProps<{
    items: DayCountOut[]
    height?: number
    loading?: boolean
    unit?: string
  }>(),
  { height: 220, loading: false, unit: '框' }
)

const { palette } = useChartTheme()

const WIDTH = 640
const PAD_LEFT = 42
const PAD_RIGHT = 14
const PAD_TOP = 14
const PAD_BOTTOM = 28

const hoverIndex = ref<number | null>(null)

const plotWidth = WIDTH - PAD_LEFT - PAD_RIGHT
const plotHeight = computed(() => props.height - PAD_TOP - PAD_BOTTOM)

const points = computed(() => {
  const items = props.items
  if (items.length === 0) return []
  const max = Math.max(1, ...items.map((item) => item.count))
  const step = items.length > 1 ? plotWidth / (items.length - 1) : 0
  return items.map((item, index) => {
    const x = PAD_LEFT + (items.length > 1 ? step * index : plotWidth / 2)
    const y = PAD_TOP + plotHeight.value - (item.count / max) * plotHeight.value
    return { x, y, label: item.date, value: item.count, index }
  })
})

const maxValue = computed(() => {
  if (props.items.length === 0) return 1
  return Math.max(1, ...props.items.map((item) => item.count))
})

const linePath = computed(() =>
  points.value.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ')
)

const areaPath = computed(() => {
  const list = points.value
  if (list.length === 0) return ''
  const first = list[0]
  const last = list[list.length - 1]
  if (!first || !last) return ''
  const baseline = PAD_TOP + plotHeight.value
  return `${linePath.value} L ${last.x.toFixed(2)} ${baseline} L ${first.x.toFixed(2)} ${baseline} Z`
})

const ticks = computed(() => {
  const max = maxValue.value
  const steps = 4
  return Array.from({ length: steps + 1 }, (_, index) => {
    const value = (max / steps) * (steps - index)
    const y = PAD_TOP + (plotHeight.value / steps) * index
    return { value, y }
  })
})

const xLabels = computed(() => {
  const list = points.value
  if (list.length === 0) return []
  const limit = 6
  if (list.length <= limit) return list
  const stride = Math.ceil(list.length / limit)
  return list.filter((_, index) => index % stride === 0)
})

const hovered = computed(() => {
  const index = hoverIndex.value
  if (index === null) return null
  return points.value[index] ?? null
})

function shortDate(value: string): string {
  if (value.length >= 10) return value.slice(5)
  return value
}
</script>

<template>
  <section class="trend-line ip-panel" aria-label="按天趋势">
    <header class="trend-line__head">
      <h3 class="ip-section-title">按天趋势</h3>
      <span class="trend-line__meta num">
        {{ items.length }} 天 · 峰值 {{ maxValue }} {{ unit }}
      </span>
    </header>

    <EmptyState
      v-if="items.length === 0"
      compact
      :description="loading ? '正在加载统计…' : '所选时间范围内没有按天数据'"
      hint=""
    />

    <div v-else class="trend-line__body">
      <svg
        class="trend-line__svg"
        :viewBox="`0 0 ${WIDTH} ${height}`"
        preserveAspectRatio="none"
        role="img"
        aria-label="按天趋势折线图"
        :style="{ height: `${height}px` }"
      >
        <line
          v-for="tick in ticks"
          :key="`grid-${tick.y}`"
          :x1="PAD_LEFT"
          :x2="WIDTH - PAD_RIGHT"
          :y1="tick.y"
          :y2="tick.y"
          :stroke="palette.line"
          stroke-width="1"
        />
        <text
          v-for="tick in ticks"
          :key="`tick-${tick.y}`"
          :x="PAD_LEFT - 6"
          :y="tick.y + 3"
          text-anchor="end"
          class="trend-line__tick"
          :fill="palette.text3"
        >
          {{ Math.round(tick.value) }}
        </text>

        <path :d="areaPath" :fill="palette.primary" opacity="0.12" />
        <path
          :d="linePath"
          fill="none"
          :stroke="palette.primary"
          stroke-width="2"
          stroke-linejoin="round"
          stroke-linecap="round"
          vector-effect="non-scaling-stroke"
        />

        <g v-for="point in points" :key="`pt-${point.index}`">
          <circle
            :cx="point.x"
            :cy="point.y"
            :r="hoverIndex === point.index ? 5 : 3"
            :fill="hoverIndex === point.index ? palette.warn : palette.primary"
            class="trend-line__dot"
            @mouseenter="hoverIndex = point.index"
            @mouseleave="hoverIndex = null"
          >
            <title>{{ point.label }}：{{ point.value }} {{ unit }}</title>
          </circle>
        </g>

        <text
          v-for="point in xLabels"
          :key="`x-${point.index}`"
          :x="point.x"
          :y="height - 8"
          text-anchor="middle"
          class="trend-line__tick"
          :fill="palette.text3"
        >
          {{ shortDate(point.label) }}
        </text>
      </svg>

      <p class="trend-line__hover num">
        {{
          hovered
            ? `${hovered.label} · ${hovered.value} ${unit}`
            : '将鼠标移到数据点上查看具体数值'
        }}
      </p>
    </div>
  </section>
</template>

<style scoped lang="scss">
.trend-line {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__meta {
    font-size: 11px;
    color: var(--ip-text-3);
  }

  &__body {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  &__svg {
    width: 100%;
    display: block;
    overflow: visible;
  }

  &__tick {
    font-family: var(--ip-font-mono);
    font-size: 11px;
  }

  &__dot {
    cursor: pointer;
  }

  &__hover {
    font-size: 11px;
    color: var(--ip-text-2);
  }
}
</style>
