<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElButton, ElEmpty, ElTable, ElTableColumn, ElTooltip } from 'element-plus'
import { formatNumber } from '@/utils'
import type { DetectionRow } from '@/types'

const props = withDefaults(
  defineProps<{
    rows: DetectionRow[]
    selectedKey: string | null
    hoveredKey: string | null
    loading?: boolean
    /** 批量模式下额外显示来源画面列 */
    showFrameColumn?: boolean
    height?: number
    emptyText?: string
    maxScore?: number | null
  }>(),
  {
    loading: false,
    showFrameColumn: true,
    height: 250,
    emptyText: '当前画面没有检测到目标',
    maxScore: null
  }
)

const emit = defineEmits<{
  (event: 'select', row: DetectionRow | null): void
  (event: 'hover', row: DetectionRow | null): void
}>()

// Element Plus 的 ElTable 是泛型组件，InstanceType<typeof ElTable> 不满足
// “abstract new (...args: any) => any” 约束；这里只用到 $el，按结构声明更稳。
const tableRef = ref<{ $el?: HTMLElement | null } | null>(null)

const sortedRows = computed(() =>
  [...props.rows].sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score
    return a.frameIndex - b.frameIndex
  })
)

function rowClass({ row }: { row: DetectionRow }): string {
  if (row.key === props.selectedKey) return 'is-picked'
  if (row.key === props.hoveredKey) return 'is-hovered'
  return ''
}

function scoreTone(score: number): string {
  if (score >= 0.7) return 'score--high'
  if (score >= 0.4) return 'score--mid'
  return 'score--low'
}

/**
 * el-table 的插槽把 row 推断为 DefaultRow（松散的 Record），
 * 直接传给强类型函数会报 TS2345。这里统一收窄一次，
 * 避免在模板里到处写 as 断言。
 */
const asDetectionRow = (row: unknown): DetectionRow => row as DetectionRow

function bboxText(row: DetectionRow): string {
  const [x1, y1, x2, y2] = row.bbox
  return `${x1.toFixed(0)},${y1.toFixed(0)} → ${x2.toFixed(0)},${y2.toFixed(0)}`
}

function onRowClick(row: DetectionRow): void {
  emit('select', row.key === props.selectedKey ? null : row)
}

/** Element Plus 的表格行 hover 事件名为 cell-mouse-enter / cell-mouse-leave */
function onCellEnter(row: DetectionRow): void {
  emit('hover', row)
}

function onCellLeave(): void {
  emit('hover', null)
}

function clearSelection(): void {
  emit('select', null)
}

// 选中变化时自动滚动到对应行，保证与画面联动可见
watch(
  () => props.selectedKey,
  async (key) => {
    if (!key) return
    await nextTick()
    const table = tableRef.value
    if (!table) return
    const row = sortedRows.value.find((item) => item.key === key)
    if (!row) return
    const rowIndex = sortedRows.value.indexOf(row)
    const body = table.$el?.querySelector('.el-table__body-wrapper') as HTMLElement | null
    if (body && rowIndex >= 0) {
      body.scrollTo({ top: Math.max(0, rowIndex * 34 - 34), behavior: 'smooth' })
    }
  }
)

const summary = computed(() => {
  const total = props.rows.length
  if (total === 0) return '0 条记录'
  const scores = props.rows.map((row) => row.score)
  const max = Math.max(...scores)
  const avg = scores.reduce((acc, value) => acc + value, 0) / total
  return `共 ${total} 条 · 最高 ${max.toFixed(2)} · 平均 ${avg.toFixed(2)}`
})
</script>

<template>
  <section class="detection-table ip-panel" aria-label="检测记录">
    <header class="detection-table__head">
      <div class="detection-table__title-group">
        <h2 class="ip-section-title">检测记录</h2>
        <span class="detection-table__summary num">{{ summary }}</span>
      </div>
      <div class="detection-table__actions">
        <span v-if="selectedKey" class="detection-table__picked">已选中 1 个目标</span>
        <ElButton
          text
          size="small"
          :disabled="!selectedKey"
          aria-label="取消选中目标"
          @click="clearSelection"
        >
          取消选中
        </ElButton>
      </div>
    </header>

    <div class="detection-table__body">
      <ElTable
        ref="tableRef"
        :data="sortedRows"
        :height="height"
        size="small"
        :row-class-name="rowClass"
        :empty-text="emptyText"
        highlight-current-row
        @row-click="onRowClick"
        @cell-mouse-enter="onCellEnter"
        @cell-mouse-leave="onCellLeave"
      >
        <ElTableColumn type="index" label="#" width="46" align="right">
          <template #default="{ $index }">
            <span class="num detection-table__index">{{ $index + 1 }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn v-if="showFrameColumn" label="画面标识" min-width="150">
          <template #default="{ row }">
            <!-- 画面标识只能是文件名或帧号，绝不是 dataURL -->
            <ElTooltip :content="row.frameLabel" placement="top">
              <span class="detection-table__frame ip-truncate">{{ row.frameLabel }}</span>
            </ElTooltip>
          </template>
        </ElTableColumn>

        <ElTableColumn label="中文名" min-width="112">
          <template #default="{ row }">
            <span class="detection-table__zh">{{ row.zhName || '—' }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="英文名" min-width="130">
          <template #default="{ row }">
            <span class="detection-table__en ip-truncate">{{ row.className }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="置信度" width="118" align="right">
          <template #default="{ row }">
            <span class="detection-table__score num" :class="scoreTone(row.score)">
              {{ row.score.toFixed(3) }}
            </span>
            <span class="detection-table__score-bar" aria-hidden="true">
              <span
                class="detection-table__score-fill"
                :class="scoreTone(row.score)"
                :style="{ width: `${Math.round(row.score * 100)}%` }"
              />
            </span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="边界框 x1,y1 → x2,y2" min-width="200">
          <template #default="{ row }">
            <span class="num detection-table__bbox">{{ bboxText(asDetectionRow(row)) }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="轨迹" width="72" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.trackId === null ? '—' : `#${row.trackId}` }}</span>
          </template>
        </ElTableColumn>

        <template #empty>
          <ElEmpty :description="emptyText" :image-size="54" />
        </template>
      </ElTable>
      <p v-if="loading" class="detection-table__loading">
        <span class="detection-table__spinner" aria-hidden="true" />
        正在检测…
      </p>
    </div>

    <footer class="detection-table__foot">
      <span class="num">检测框合计 {{ formatNumber(rows.length) }}</span>
      <span v-if="maxScore !== null" class="num">画面最高分 {{ maxScore.toFixed(3) }}</span>
    </footer>
  </section>
</template>

<style scoped lang="scss">
.detection-table {
  display: flex;
  flex-direction: column;
  min-height: 0;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--ip-line);
  }

  &__title-group {
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-width: 0;
  }

  &__summary {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__picked {
    font-size: var(--ip-fs-xs);
    color: var(--ip-primary-hover);
  }

  &__body {
    position: relative;
    min-height: 0;
  }

  &__index {
    color: var(--ip-text-3);
  }

  &__frame {
    display: inline-block;
    max-width: 100%;
    font-family: var(--ip-font-mono);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__zh {
    color: var(--ip-text);
  }

  &__en {
    display: inline-block;
    max-width: 100%;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__score {
    font-size: var(--ip-fs-xs);
  }

  &__score--high {
    color: var(--ip-primary-hover);
  }

  &__score--mid {
    color: var(--ip-warn);
  }

  &__score--low {
    color: var(--ip-text-3);
  }

  &__score-bar {
    display: block;
    height: 3px;
    margin-top: 2px;
    border-radius: 2px;
    background: var(--ip-panel-3);
    overflow: hidden;
  }

  &__score-fill {
    display: block;
    height: 100%;
    background: var(--ip-text-3);
  }

  &__score-fill.detection-table__score--high {
    background: var(--ip-primary);
  }

  &__score-fill.detection-table__score--mid {
    background: var(--ip-warn);
  }

  &__bbox {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__loading {
    position: absolute;
    right: 12px;
    top: 8px;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 9px;
    border-radius: 999px;
    background: var(--ip-panel);
    border: 1px solid var(--ip-line);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__spinner {
    width: 11px;
    height: 11px;
    border: 2px solid var(--ip-line-strong);
    border-top-color: var(--ip-primary);
    border-radius: 50%;
    animation: table-spin 720ms linear infinite;
  }

  &__foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 12px;
    border-top: 1px solid var(--ip-line);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }
}

/* 表格：斑马纹关闭，hover 淡绿，选中行左侧 3px 绿色指示条 */
:deep(.el-table__row) {
  cursor: pointer;
}

:deep(.el-table__row.is-hovered),
:deep(.el-table__row.is-picked) {
  background: var(--ip-primary-weak);
}

:deep(.el-table__row.is-picked td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 0 var(--ip-primary);
}

:deep(.el-table__row--striped) {
  background: transparent;
}

@keyframes table-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
