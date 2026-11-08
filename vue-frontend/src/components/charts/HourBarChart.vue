<script setup lang="ts">
import { computed, ref } from 'vue'
import { useChartTheme } from '@/composables/useChartTheme'

const props = withDefaults(
  defineProps<{
    /** 长度 24 的数组 */
    hours: number[]
    height?: number
    loading?: boolean
    unit?: string
  }>(),
  { height: 200, loading: false, unit: '框' }
)

const { palette } = useChartTheme()
const hoverIndex = ref<number | null>(null)

const bars = computed(() => {
  const values = Array.from({ length: 24 }, (_, index) => props.hours[index] ?? 0)
  const max = Math.max(1, ...values)
  return values.map((value, index) => ({
    hour: index,
    value,
    ratio: value / max
  }))
})

const maxValue = computed(() => Math.max(1, ...bars.value.map((bar) => bar.value)))
const peak = computed(() => {
  const sorted = [...bars.value].sort((a, b) => b.value - a.value)
  return sorted[0] ?? null
})

function barHeight(ratio: number): number {
  const usable = props.height - 34
  if (ratio <= 0) return 2
  return Math.max(3, ratio * usable)
}

const hovered = computed(() => {
  const index = hoverIndex.value
  if (index === null) return null
  return bars.value[index] ?? null
})

function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, '0')}:00`
}
</script>

<template>
  <section class="hour-bar ip-panel" aria-label="24 小时分布">
    <header class="hour-bar__head">
      <h3 class="ip-section-title">24 小时分布</h3>
      <span class="hour-bar__meta num">
        <template v-if="peak && peak.value > 0">
          峰值 {{ hourLabel(peak.hour) }} · {{ peak.value }} {{ unit }}
        </template>
        <template v-else>{{ loading ? '加载中…' : '暂无数据' }}</template>
      </span>
    </header>

    <div class="hour-bar__chart" :style="{ height: `${height}px` }">
      <div
        v-for="bar in bars"
        :key="bar.hour"
        class="hour-bar__col"
        :class="{ 'is-hovered': hoverIndex === bar.hour }"
        :title="`${hourLabel(bar.hour)} · ${bar.value} ${unit}`"
        @mouseenter="hoverIndex = bar.hour"
        @mouseleave="hoverIndex = null"
      >
        <span class="hour-bar__value num">{{ bar.value > 0 ? bar.value : '' }}</span>
        <span
          class="hour-bar__bar"
          :style="{
            height: `${barHeight(bar.ratio)}px`,
            background: hoverIndex === bar.hour ? palette.warn : palette.primary
          }"
        />
        <span class="hour-bar__label num">{{ String(bar.hour).padStart(2, '0') }}</span>
      </div>
    </div>

    <p class="hour-bar__hover num">
      {{ hovered ? `${hourLabel(hovered.hour)} · ${hovered.value} ${unit}` : '横轴为 0-23 时（本地时间），纵轴为该小时的框数' }}
    </p>
  </section>
</template>

<style scoped lang="scss">
.hour-bar {
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

  &__chart {
    display: flex;
    align-items: flex-end;
    gap: 3px;
    padding-top: 14px;
    border-bottom: 1px solid var(--ip-line);
  }

  &__col {
    position: relative;
    flex: 1 1 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: flex-end;
    height: 100%;
    cursor: default;
  }

  &__col.is-hovered .hour-bar__value {
    color: var(--ip-text);
  }

  &__value {
    font-size: 11px;
    color: var(--ip-text-3);
    line-height: 1;
    margin-bottom: 2px;
  }

  &__bar {
    display: block;
    width: 100%;
    max-width: 22px;
    border-radius: 2px 2px 0 0;
    transition: background var(--ip-dur-fast) var(--ip-ease);
  }

  &__label {
    margin-top: 3px;
    font-size: 11px;
    color: var(--ip-text-3);
    transform: translateY(100%);
    position: absolute;
    bottom: -16px;
  }

  &__hover {
    margin-top: 8px;
    font-size: 11px;
    color: var(--ip-text-2);
  }
}
</style>
