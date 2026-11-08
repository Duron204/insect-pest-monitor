<script setup lang="ts">
import { computed, ref } from 'vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { useChartTheme } from '@/composables/useChartTheme'
import type { ClassCountOut } from '@/types'

const props = withDefaults(
  defineProps<{
    items: ClassCountOut[]
    /** 显示前 N 项，其余合并为「其它」 */
    topN?: number
    size?: number
    loading?: boolean
  }>(),
  { topN: 8, size: 220, loading: false }
)

const { colorAt, palette } = useChartTheme()
const hoveredIndex = ref<number | null>(null)

const RADIUS = 70
const INNER = 44
const CENTER = 80

interface Slice {
  label: string
  value: number
  color: string
  path: string
  percent: number
}

const total = computed(() => props.items.reduce((acc, item) => acc + item.count, 0))

function polar(cx: number, cy: number, radius: number, angle: number): [number, number] {
  return [cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)]
}

function donutPath(startAngle: number, endAngle: number): string {
  const [x1, y1] = polar(CENTER, CENTER, RADIUS, startAngle)
  const [x2, y2] = polar(CENTER, CENTER, RADIUS, endAngle)
  const [x3, y3] = polar(CENTER, CENTER, INNER, endAngle)
  const [x4, y4] = polar(CENTER, CENTER, INNER, startAngle)
  const largeArc = endAngle - startAngle > Math.PI ? 1 : 0
  return [
    `M ${x1.toFixed(2)} ${y1.toFixed(2)}`,
    `A ${RADIUS} ${RADIUS} 0 ${largeArc} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`,
    `L ${x3.toFixed(2)} ${y3.toFixed(2)}`,
    `A ${INNER} ${INNER} 0 ${largeArc} 0 ${x4.toFixed(2)} ${y4.toFixed(2)}`,
    'Z'
  ].join(' ')
}

const slices = computed<Slice[]>(() => {
  const sum = total.value
  if (sum <= 0) return []
  const sorted = [...props.items].sort((a, b) => b.count - a.count)
  const head = sorted.slice(0, props.topN)
  const tail = sorted.slice(props.topN)
  const merged = tail.length > 0 ? [...head, { zh_name: '其它', en_name: 'others', count: tail.reduce((acc, item) => acc + item.count, 0) }] : head

  let angle = -Math.PI / 2
  return merged.map((item, index) => {
    const ratio = item.count / sum
    const start = angle
    const end = angle + ratio * Math.PI * 2
    angle = end
    return {
      label: item.zh_name,
      value: item.count,
      color: colorAt(index),
      path: donutPath(start, end),
      percent: ratio * 100
    }
  })
})

const hovered = computed(() => {
  const index = hoveredIndex.value
  if (index === null) return null
  return slices.value[index] ?? null
})
</script>

<template>
  <section class="class-pie ip-panel" aria-label="类别分布">
    <header class="class-pie__head">
      <h3 class="ip-section-title">类别分布</h3>
      <span class="class-pie__total num">合计 {{ total }}</span>
    </header>

    <EmptyState
      v-if="slices.length === 0"
      compact
      :description="loading ? '正在加载统计…' : '所选时间范围内没有检出记录'"
      :hint="loading ? '' : '换个时间范围，或先在检测台跑一次检测'"
    />

    <div v-else class="class-pie__body">
      <svg
        class="class-pie__svg"
        :viewBox="`0 0 160 160`"
        :width="size"
        :height="size"
        role="img"
        aria-label="类别分布环形图"
      >
        <circle :cx="CENTER" :cy="CENTER" :r="RADIUS" fill="none" :stroke="palette.line" stroke-width="1" />
        <path
          v-for="(slice, index) in slices"
          :key="slice.label"
          :d="slice.path"
          :fill="slice.color"
          :opacity="hoveredIndex === null || hoveredIndex === index ? 0.92 : 0.38"
          class="class-pie__slice"
          @mouseenter="hoveredIndex = index"
          @mouseleave="hoveredIndex = null"
        >
          <title>{{ slice.label }}：{{ slice.value }}（{{ slice.percent.toFixed(1) }}%）</title>
        </path>
        <text
          :x="CENTER"
          :y="CENTER - 4"
          text-anchor="middle"
          class="class-pie__center-value"
          :fill="palette.text"
        >
          {{ hovered ? hovered.value : total }}
        </text>
        <text
          :x="CENTER"
          :y="CENTER + 13"
          text-anchor="middle"
          class="class-pie__center-label"
          :fill="palette.text3"
        >
          {{ hovered ? hovered.label : '框总数' }}
        </text>
      </svg>

      <ul class="class-pie__legend">
        <li
          v-for="(slice, index) in slices"
          :key="slice.label"
          class="class-pie__legend-item"
          :class="{ 'is-hovered': hoveredIndex === index }"
          @mouseenter="hoveredIndex = index"
          @mouseleave="hoveredIndex = null"
        >
          <span class="class-pie__dot" :style="{ background: slice.color }" aria-hidden="true" />
          <span class="class-pie__legend-name ip-truncate" :title="slice.label">{{ slice.label }}</span>
          <span class="class-pie__legend-count num">{{ slice.value }}</span>
          <span class="class-pie__legend-percent num">{{ slice.percent.toFixed(1) }}%</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<style scoped lang="scss">
.class-pie {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__total {
    font-size: 11px;
    color: var(--ip-text-3);
  }

  &__body {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }

  &__svg {
    flex: 0 0 auto;
  }

  &__slice {
    cursor: pointer;
    transition: opacity var(--ip-dur-fast) var(--ip-ease);
  }

  &__center-value {
    font-family: var(--ip-font-mono);
    font-size: 20px;
    font-weight: 600;
  }

  &__center-label {
    font-size: 11px;
  }

  &__legend {
    flex: 1 1 150px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
    max-height: 210px;
    overflow-y: auto;
  }

  &__legend-item {
    display: grid;
    grid-template-columns: 8px minmax(0, 1fr) 44px 48px;
    align-items: center;
    gap: 7px;
    padding: 2px 4px;
    border-radius: 4px;
    font-size: 12px;
    color: var(--ip-text-2);
  }

  &__legend-item.is-hovered {
    background: var(--ip-primary-weak);
    color: var(--ip-text);
  }

  &__dot {
    width: 8px;
    height: 8px;
    border-radius: 2px;
  }

  &__legend-name {
    min-width: 0;
  }

  &__legend-count {
    text-align: right;
    color: var(--ip-text);
  }

  &__legend-percent {
    text-align: right;
    font-size: 11px;
    color: var(--ip-text-3);
  }
}
</style>
