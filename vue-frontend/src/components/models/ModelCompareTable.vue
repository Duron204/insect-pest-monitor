<script setup lang="ts">
import { computed } from 'vue'
import { ElTable, ElTableColumn, ElTag } from 'element-plus'
import { formatNumber, formatRatio } from '@/utils'
import type { ModelOut } from '@/types'

const props = withDefaults(
  defineProps<{
    models: ModelOut[]
    currentPath: string | null
    recommendedId: number | null
    currentModelId: number | null
  }>(),
  { currentPath: null, recommendedId: null, currentModelId: null }
)

const ranked = computed(() =>
  [...props.models].sort((a, b) => {
    const left = a.map50_95 ?? a.map50 ?? -1
    const right = b.map50_95 ?? b.map50 ?? -1
    return right - left
  })
)

/** el-table 的插槽把 row 推断为 DefaultRow，传给强类型函数前先统一收窄。 */
const asModel = (row: unknown): ModelOut => row as ModelOut

function isCurrent(model: ModelOut): boolean {
  if (props.currentModelId !== null && model.id === props.currentModelId) return true
  return props.currentPath !== null && model.path === props.currentPath
}

function rowClass({ row }: { row: ModelOut }): string {
  if (isCurrent(row)) return 'is-current-model'
  if (props.recommendedId !== null && row.id === props.recommendedId) return 'is-recommended-model'
  return ''
}

function delta(model: ModelOut): string {
  const current = props.models.find(
    (item) => item.id === props.currentModelId || (props.currentPath !== null && item.path === props.currentPath)
  )
  if (!current || isCurrent(model)) return '—'
  const left = current.map50_95 ?? current.map50
  const right = model.map50_95 ?? model.map50
  if (left === null || left === undefined || right === null || right === undefined) return '—'
  const diff = right - left
  const sign = diff > 0 ? '+' : ''
  return `${sign}${diff.toFixed(3)}`
}
</script>

<template>
  <section class="model-compare ip-panel" aria-label="模型精度对比">
    <header class="model-compare__head">
      <h3 class="ip-section-title">精度对比（按 mAP 排序）</h3>
      <span class="model-compare__note">相对当前模型的差值</span>
    </header>
    <ElTable :data="ranked" size="small" :row-class-name="rowClass" max-height="360">
      <ElTableColumn label="#" width="46" align="right">
        <template #default="{ $index }">
          <span class="num model-compare__index">{{ $index + 1 }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="模型" min-width="180">
        <template #default="{ row }">
          <div class="model-compare__name-cell">
            <span class="ip-truncate" :title="row.name">{{ row.name }}</span>
            <ElTag v-if="isCurrent(asModel(row))" size="small" type="success" effect="plain">当前</ElTag>
            <ElTag
              v-else-if="recommendedId !== null && row.id === recommendedId"
              size="small"
              type="warning"
              effect="plain"
            >
              推荐
            </ElTag>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="架构" width="118">
        <template #default="{ row }">{{ row.arch || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="mAP50" width="92" align="right">
        <template #default="{ row }">
          <span class="num">{{ row.map50 === null ? '—' : formatRatio(row.map50, 3) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="mAP50-95" width="104" align="right">
        <template #default="{ row }">
          <span class="num">{{ row.map50_95 === null ? '—' : formatRatio(row.map50_95, 3) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="相对当前" width="104" align="right">
        <template #default="{ row }">
          <span class="num model-compare__delta">{{ delta(asModel(row)) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Epochs" width="88" align="right">
        <template #default="{ row }">
          <span class="num">{{ row.epochs === null ? '—' : formatNumber(row.epochs) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="来源" width="118">
        <template #default="{ row }">{{ row.source === 'weights' ? 'weights' : row.source === 'runs' ? 'runs' : row.source }}</template>
      </ElTableColumn>
    </ElTable>
  </section>
</template>

<style scoped lang="scss">
.model-compare {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__note {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__index {
    color: var(--ip-text-3);
  }

  &__name-cell {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  &__delta {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }
}

:deep(.el-table__row.is-current-model) {
  background: var(--ip-primary-weak);
}

:deep(.el-table__row.is-recommended-model) {
  background: var(--ip-warn-weak);
}
</style>
