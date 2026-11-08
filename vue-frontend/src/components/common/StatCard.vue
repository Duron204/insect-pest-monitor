<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    label: string
    value: number | string | null | undefined
    unit?: string
    hint?: string
    /** 强调数字（去重虫数用） */
    emphasis?: boolean
    tone?: 'default' | 'primary' | 'warn' | 'info'
    loading?: boolean
    digits?: number
  }>(),
  {
    unit: '',
    hint: '',
    emphasis: false,
    tone: 'default',
    loading: false,
    digits: 0
  }
)

const displayValue = computed(() => {
  const raw = props.value
  if (raw === null || raw === undefined || raw === '') return '—'
  if (typeof raw === 'number') {
    if (!Number.isFinite(raw)) return '—'
    return raw.toLocaleString('zh-CN', {
      minimumFractionDigits: props.digits,
      maximumFractionDigits: props.digits
    })
  }
  return raw
})
</script>

<template>
  <div class="stat-card" :class="[`stat-card--${tone}`, { 'stat-card--emphasis': emphasis }]">
    <p class="stat-card__label">{{ label }}</p>
    <div class="stat-card__value-row">
      <span v-if="loading" class="stat-card__skeleton" aria-hidden="true" />
      <span v-else class="stat-card__value num">{{ displayValue }}</span>
      <span v-if="unit && !loading" class="stat-card__unit">{{ unit }}</span>
    </div>
    <p v-if="hint" class="stat-card__hint">{{ hint }}</p>
  </div>
</template>

<style scoped lang="scss">
.stat-card {
  position: relative;
  padding: 12px 14px;
  background: var(--ip-panel);
  border: 1px solid var(--ip-line);
  border-radius: var(--ip-radius);
  /* 克制：细边框 + 微弱内阴影，不做彩色大渐变 */
  box-shadow: var(--ip-inner);

  &__label {
    font-size: var(--ip-fs-xs);
    letter-spacing: 0.04em;
    color: var(--ip-text-2);
  }

  &__value-row {
    display: flex;
    align-items: baseline;
    gap: 4px;
    margin-top: 4px;
    min-height: 30px;
  }

  &__value {
    font-size: 24px;
    line-height: 1.15;
    font-weight: 600;
    color: var(--ip-text);
    letter-spacing: -0.01em;
  }

  &__unit {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__hint {
    margin-top: 2px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__skeleton {
    display: inline-block;
    width: 64px;
    height: 22px;
    border-radius: 4px;
    background: var(--ip-panel-3);
  }

  &--emphasis {
    border-color: var(--ip-primary-line);

    .stat-card__value {
      color: var(--ip-primary-hover);
      font-size: 28px;
    }

    &::after {
      content: '';
      position: absolute;
      left: 0;
      top: 10px;
      bottom: 10px;
      width: 3px;
      border-radius: 0 2px 2px 0;
      background: var(--ip-primary);
    }
  }

  &--warn {
    border-color: var(--ip-warn-line);

    .stat-card__value {
      color: var(--ip-warn);
    }
  }

  &--info {
    .stat-card__value {
      color: var(--ip-info);
    }
  }

  &--primary {
    .stat-card__value {
      color: var(--ip-primary-hover);
    }
  }
}
</style>
