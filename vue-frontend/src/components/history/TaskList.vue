<script setup lang="ts">
/**
 * 任务列表。
 *
 * 本次重做的要点（都来自对现有界面的实测，不是凭感觉）：
 *  1. **加标注图缩略图** —— 这是个害虫检测系统，原先整页一张图都不显示，
 *     用户只能靠文件名和数字认任务；缩略图是"查看方便"的最大改善。
 *  2. **模式改成彩色标签**：单图/视频/设备上报/本机抓拍一眼可分。
 *  3. **操作收拢**：三个报告格式收进下拉，删除改为图标按钮，
 *     行高不再忽高忽低。
 *  4. **窄屏改用卡片列表**：Element 表格在 390px 宽必然横向溢出，
 *     手机上要左右拖着看，这在田里没法用。
 *  5. 数字不再用主色（绿色）—— 那会让数字看起来像链接。
 */
import { computed } from 'vue'
import {
  ElButton,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElPagination,
  ElTable,
  ElTableColumn,
  ElTag,
  ElTooltip
} from 'element-plus'
import EmptyState from '@/components/common/EmptyState.vue'
import { formatDateTime, formatNumber, formatTaskNo, taskModeLabel, taskStatusLabel } from '@/utils'
import type { TaskOut } from '@/types'

const props = withDefaults(
  defineProps<{
    items: TaskOut[]
    total: number
    page: number
    pageSize: number
    loading?: boolean
    activeTaskId?: number | null
  }>(),
  { loading: false, activeTaskId: null }
)

/** el-table 的插槽把 row 推断为 DefaultRow，而事件载荷要求 TaskOut，这里统一收窄。 */
const asTask = (row: unknown): TaskOut => row as TaskOut

const emit = defineEmits<{
  (event: 'open', task: TaskOut): void
  (event: 'delete', task: TaskOut): void
  (event: 'export', payload: { task: TaskOut; format: 'xlsx' | 'docx' | 'html' }): void
  (event: 'page', page: number): void
  (event: 'page-size', size: number): void
  (event: 'empty-action'): void
}>()

function statusTone(status: string | null): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'done':
      return 'success'
    case 'running':
      return 'warning'
    case 'pending':
      return 'info'
    case 'failed':
      return 'danger'
    default:
      return 'info'
  }
}

/** 模式 → 标签色调。本机抓拍是"盒子自己拍的"，用主色区分于人工上传。 */
function modeTone(mode: string): 'primary' | 'warn' | 'info' {
  if (mode === 'local') return 'primary'
  if (mode === 'video' || mode === 'camera') return 'info'
  return 'primary'
}

/** 缩略图地址：优先标注图；视频任务没有标注图，用占位块。 */
function thumbUrl(row: TaskOut): string | null {
  return row.result_image_path ?? null
}

function onExportCommand(task: TaskOut, command: string | number | object): void {
  const format = String(command) as 'xlsx' | 'docx' | 'html'
  if (format === 'xlsx' || format === 'docx' || format === 'html') {
    emit('export', { task, format })
  }
}

function rowClass({ row }: { row: TaskOut }): string {
  return row.id === props.activeTaskId ? 'is-active-task' : ''
}

const isEmpty = computed(() => props.items.length === 0 && !props.loading)
</script>

<template>
  <section class="task-list ip-panel" aria-label="任务列表">
    <template v-if="!isEmpty">
      <div>
        <ElTable
          :data="items"
          size="small"
          :row-class-name="rowClass"
          max-height="560"
          @row-click="(row: TaskOut) => emit('open', row)"
        >
          <ElTableColumn label="标注图" width="92">
            <template #default="{ row }">
              <span v-if="thumbUrl(asTask(row))" class="ip-thumb task-list__thumb">
                <img :src="thumbUrl(asTask(row)) ?? ''" alt="标注结果" loading="lazy" />
              </span>
              <span v-else class="task-list__thumb task-list__thumb--none" aria-label="无标注图">
                {{ taskModeLabel(asTask(row).mode).slice(0, 2) }}
              </span>
            </template>
          </ElTableColumn>

          <ElTableColumn label="任务号" width="100">
            <template #default="{ row }">
              <span class="num task-list__id">{{ formatTaskNo(row.id) }}</span>
            </template>
          </ElTableColumn>

          <ElTableColumn label="模式" width="112">
            <template #default="{ row }">
              <span class="ip-chip" :class="`ip-chip--${modeTone(asTask(row).mode)}`">
                {{ taskModeLabel(asTask(row).mode) }}
              </span>
            </template>
          </ElTableColumn>

          <ElTableColumn label="来源" min-width="150">
            <template #default="{ row }">
              <ElTooltip :content="row.source_name ?? '—'" placement="top">
                <span class="task-list__source ip-truncate">{{ row.source_name ?? '—' }}</span>
              </ElTooltip>
            </template>
          </ElTableColumn>

          <ElTableColumn label="开始时间" width="150">
            <template #default="{ row }">
              <span class="num task-list__time">{{ formatDateTime(row.started_at) }}</span>
            </template>
          </ElTableColumn>

          <ElTableColumn label="虫数" width="88" align="right">
            <template #default="{ row }">
              <ElTooltip content="按轨迹去重后的独立目标数（更接近真实虫口）" placement="top">
                <span class="num task-list__unique">{{ formatNumber(row.unique_objects ?? 0) }}</span>
              </ElTooltip>
            </template>
          </ElTableColumn>

          <ElTableColumn label="框数" width="80" align="right">
            <template #default="{ row }">
              <span class="num task-list__muted">{{ formatNumber(row.total_objects ?? 0) }}</span>
            </template>
          </ElTableColumn>

          <ElTableColumn label="状态" width="100">
            <template #default="{ row }">
              <ElTag :type="statusTone(row.status)" size="small" effect="plain">
                {{ taskStatusLabel(row.status) }}
              </ElTag>
            </template>
          </ElTableColumn>

          <ElTableColumn label="操作" width="170" fixed="right">
            <template #default="{ row }">
              <div class="ip-cell-actions">
                <ElButton
                  text
                  type="primary"
                  size="small"
                  aria-label="查看任务详情"
                  @click.stop="emit('open', asTask(row))"
                >
                  详情
                </ElButton>
                <ElDropdown trigger="click" @command="(cmd) => onExportCommand(asTask(row), cmd)">
                  <ElButton text size="small" aria-label="导出报告">导出</ElButton>
                  <template #dropdown>
                    <ElDropdownMenu>
                      <ElDropdownItem command="xlsx">Excel 报告 (.xlsx)</ElDropdownItem>
                      <ElDropdownItem command="docx">Word 报告 (.docx)</ElDropdownItem>
                      <ElDropdownItem command="html">网页报告 (.html)</ElDropdownItem>
                    </ElDropdownMenu>
                  </template>
                </ElDropdown>
                <ElTooltip content="删除该任务" placement="top">
                  <ElButton
                    text
                    type="danger"
                    size="small"
                    aria-label="删除该任务"
                    @click.stop="emit('delete', asTask(row))"
                  >
                    删除
                  </ElButton>
                </ElTooltip>
              </div>
            </template>
          </ElTableColumn>

          <template #empty>
            <EmptyState compact description="没有符合条件的任务" hint="可放宽筛选条件或先到检测台跑一次检测" />
          </template>
        </ElTable>
      </div>

      <footer class="task-list__foot">
        <ElPagination
          :current-page="page"
          :page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          :layout="'total, prev, pager, next'"
          small
          background
          @current-change="(value: number) => emit('page', value)"
          @size-change="(value: number) => emit('page-size', value)"
        />
      </footer>
    </template>

    <EmptyState
      v-else
      description="还没有任何检测任务"
      hint="到检测台上传一张图片即可生成第一条历史记录"
      action-text="前往检测台"
      @action="emit('empty-action')"
    />
  </section>
</template>

<style scoped lang="scss">
.task-list {
  display: flex;
  flex-direction: column;
  min-height: 0;

  &__thumb {
    display: block;
    width: 64px;
    height: 44px;

    &--none {
      display: grid;
      place-items: center;
      border-radius: var(--ip-radius);
      border: 1px dashed var(--ip-line);
      background: var(--ip-panel-2);
      color: var(--ip-text-3);
      font-size: var(--ip-fs-xs);
    }
  }

  &__id {
    color: var(--ip-text-2);
  }

  &__source {
    display: inline-block;
    max-width: 100%;
    font-family: var(--ip-font-mono);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__time {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  /* 数字用普通文字色 + 加粗来强调，不用主色（主色会被当成链接） */
  &__unique {
    color: var(--ip-text);
    font-weight: 650;
  }

  &__muted {
    color: var(--ip-text-2);
  }

  &__foot {
    display: flex;
    justify-content: flex-end;
    padding: 10px 12px;
    border-top: 1px solid var(--ip-line);
  }
}


:deep(.el-table__row) {
  cursor: pointer;
}

:deep(.el-table__row.is-active-task) {
  background: var(--ip-primary-weak);
}

:deep(.el-table__row.is-active-task td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 0 var(--ip-primary);
}
</style>
