<script setup lang="ts">
import { computed } from 'vue'
import { ElButton, ElTag, ElTooltip } from 'element-plus'
import { formatDateTime, formatNumber, formatRatio } from '@/utils'
import { baseName } from '@/api/models'
import type { ModelOut } from '@/types'

const props = withDefaults(
  defineProps<{
    model: ModelOut
    isCurrent: boolean
    isRecommended: boolean
    switching?: boolean
  }>(),
  { switching: false }
)

const emit = defineEmits<{
  (event: 'select', model: ModelOut): void
}>()

const fileLabel = computed(() => baseName(props.model.path) || props.model.name)

const map50Text = computed(() => (props.model.map50 === null ? '—' : formatRatio(props.model.map50, 3)))
const map5095Text = computed(() =>
  props.model.map50_95 === null ? '—' : formatRatio(props.model.map50_95, 3)
)

const sourceLabel = computed(() => {
  switch (props.model.source) {
    case 'weights':
      return 'weights 目录'
    case 'runs':
      return 'runs 训练产物'
    case 'edge':
      return '边缘 ONNX（树莓派用）'
    default:
      return props.model.source
  }
})

const reason = computed(() => {
  if (props.isRecommended) {
    const score = props.model.map50_95 ?? props.model.map50
    if (score !== null) return `精度最高（mAP ${formatRatio(score, 3)}），建议切换`
    return '精度最高，建议切换'
  }
  if (props.isCurrent) return '当前正在使用'
  return ''
})

/**
 * 训练时间：后端给的是 ISO 串（`2026-06-27T20:11:18.31`）。
 * 直接渲染会又长又被列宽折断（还会丢掉后半截），这里统一走 formatDateTime，
 * 与历史页的写法保持一致；null 由它返回 `—`。
 */
const trainedAtText = computed(() => formatDateTime(props.model.trained_at))

/** 部署档：后端存的是 `pc` / `edge`，直接显示英文单词不好懂。 */
const profileText = computed(() => {
  switch (props.model.profile) {
    case 'pc':
      return 'PC 端'
    case 'edge':
      return '边缘端（树莓派）'
    default:
      return props.model.profile ?? '—'
  }
})

function onSelect(): void {
  if (props.isCurrent || props.switching) return
  emit('select', props.model)
}
</script>

<template>
  <article
    class="model-card"
    :class="{
      'model-card--current': isCurrent,
      'model-card--recommended': isRecommended && !isCurrent,
      'model-card--missing': !model.exists
    }"
  >
    <header class="model-card__head">
      <div class="model-card__title-group">
        <h3 class="model-card__name ip-truncate" :title="model.name">{{ model.name }}</h3>
        <div class="model-card__tags">
          <ElTag v-if="isCurrent" size="small" type="success" effect="plain">当前模型</ElTag>
          <ElTag v-if="isRecommended" size="small" type="warning" effect="plain">
            精度最高 · 建议切换
          </ElTag>
          <ElTag v-if="model.is_default" size="small" effect="plain">默认</ElTag>
          <ElTag v-if="!model.exists" size="small" type="danger" effect="plain">文件缺失</ElTag>
        </div>
      </div>
      <span class="model-card__id num">#{{ model.id }}</span>
    </header>

    <p v-if="reason" class="model-card__reason">{{ reason }}</p>

    <dl class="model-card__metrics">
      <div>
        <dt>架构</dt>
        <dd>{{ model.arch || '—' }}</dd>
      </div>
      <div>
        <dt>mAP50</dt>
        <dd class="num">{{ map50Text }}</dd>
      </div>
      <div>
        <dt>mAP50-95</dt>
        <dd class="num">{{ map5095Text }}</dd>
      </div>
      <div>
        <dt>Epochs</dt>
        <dd class="num">{{ model.epochs === null ? '—' : formatNumber(model.epochs) }}</dd>
      </div>
      <div>
        <dt>来源</dt>
        <dd>{{ sourceLabel }}</dd>
      </div>
      <div>
        <dt>训练时间</dt>
        <dd class="num model-card__time">{{ trainedAtText }}</dd>
      </div>
      <div v-if="model.profile" class="model-card__metrics-wide">
        <dt>部署档</dt>
        <dd>{{ profileText }}</dd>
      </div>
      <div class="model-card__metrics-wide">
        <dt>权重文件</dt>
        <dd class="ip-truncate" :title="model.path">
          <ElTooltip :content="model.path" placement="top">
            <span class="num">{{ fileLabel }}</span>
          </ElTooltip>
        </dd>
      </div>
    </dl>

    <footer class="model-card__foot">
      <ElButton
        v-if="!isCurrent"
        size="small"
        :type="isRecommended ? 'warning' : 'default'"
        :plain="!isRecommended"
        :disabled="!model.exists"
        :loading="switching"
        :aria-label="`切换到模型 ${model.name}`"
        @click="onSelect"
      >
        {{ model.exists ? '切换到此模型' : '权重文件缺失' }}
      </ElButton>
      <span v-else class="model-card__inuse">
        <span class="ip-dot ip-dot--ok" aria-hidden="true" />
        正在使用
      </span>
    </footer>
  </article>
</template>

<style scoped lang="scss">
.model-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  background: var(--ip-panel);
  border: 1px solid var(--ip-line);
  border-radius: var(--ip-radius);
  box-shadow: var(--ip-inner);

  &--current {
    border-color: var(--ip-primary-line);
  }

  &--recommended {
    border-color: var(--ip-warn-line);
  }

  &--missing {
    opacity: 0.72;
  }

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
  }

  &__title-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  &__name {
    font-size: var(--ip-fs-sm);
    font-weight: 600;
    color: var(--ip-text);
    max-width: 100%;
  }

  &__tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  &__id {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    flex: 0 0 auto;
  }

  &__reason {
    font-size: var(--ip-fs-xs);
    color: var(--ip-warn);
  }

  &__metrics {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 5px 10px;
    margin: 0;

    div {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      min-width: 0;
    }

    dt {
      font-size: var(--ip-fs-xs);
      color: var(--ip-text-3);
      flex: 0 0 auto;
    }

    dd {
      margin: 0;
      font-size: var(--ip-fs-xs);
      color: var(--ip-text);
      text-align: right;
      min-width: 0;
    }
  }

  &__metrics-wide {
    grid-column: 1 / -1;
  }

  &__foot {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding-top: 6px;
    border-top: 1px solid var(--ip-line-soft);
  }

  &__inuse {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-primary-hover);
  }
}
/** 训练时间不要被列宽折断（原先直接渲染原始 ISO 串，会断成两行还丢尾巴） */
.model-card__time {
  white-space: nowrap;
}
</style>
