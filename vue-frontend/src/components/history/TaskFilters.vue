<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElButton, ElDatePicker, ElOption, ElSelect } from 'element-plus'

const props = withDefaults(
  defineProps<{
    mode: string
    status: string
    dateRange: [string, string] | null
    loading?: boolean
    total: number
  }>(),
  { loading: false }
)

const emit = defineEmits<{
  (event: 'apply', payload: { mode: string; status: string; dateRange: [string, string] | null }): void
  (event: 'reset'): void
}>()

const MODE_OPTIONS = [
  { value: 'image', label: '单图检测' },
  { value: 'folder', label: '批量检测' },
  { value: 'video', label: '视频检测' },
  { value: 'camera', label: '摄像头实时' },
  { value: 'local', label: '本机抓拍' },
  // 绑定的树莓派上报的定时抓拍（文案与 utils 的 TASK_MODE_LABELS 保持一致）
  { value: 'device', label: '设备上报' }
]

const STATUS_OPTIONS = [
  { value: 'pending', label: '排队中' },
  { value: 'running', label: '进行中' },
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '失败' }
]

const localMode = ref(props.mode)
const localStatus = ref(props.status)
const localRange = ref<[string, string] | null>(props.dateRange)

watch(
  () => [props.mode, props.status, props.dateRange] as const,
  ([mode, status, range]) => {
    localMode.value = mode
    localStatus.value = status
    localRange.value = range
  }
)

function apply(): void {
  emit('apply', {
    mode: localMode.value,
    status: localStatus.value,
    dateRange: localRange.value
  })
}

function reset(): void {
  localMode.value = ''
  localStatus.value = ''
  localRange.value = null
  emit('reset')
}
</script>

<template>
  <section class="task-filters ip-panel" aria-label="历史记录筛选">
    <div class="task-filters__fields">
      <div class="task-filters__field">
        <label class="ip-label" for="filter-mode">检测模式</label>
        <ElSelect
          id="filter-mode"
          v-model="localMode"
          size="small"
          clearable
          placeholder="全部模式"
          aria-label="按检测模式筛选"
        >
          <ElOption
            v-for="option in MODE_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </ElSelect>
      </div>

      <div class="task-filters__field">
        <label class="ip-label" for="filter-status">任务状态</label>
        <ElSelect
          id="filter-status"
          v-model="localStatus"
          size="small"
          clearable
          placeholder="全部状态"
          aria-label="按任务状态筛选"
        >
          <ElOption
            v-for="option in STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </ElSelect>
      </div>

      <div class="task-filters__field task-filters__field--wide">
        <label class="ip-label" for="filter-range">起止时间</label>
        <ElDatePicker
          id="filter-range"
          v-model="localRange"
          type="datetimerange"
          size="small"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          value-format="YYYY-MM-DDTHH:mm:ss"
          aria-label="按时间范围筛选"
        />
      </div>
    </div>

    <div class="task-filters__actions">
      <ElButton size="small" :loading="loading" aria-label="应用筛选条件" @click="apply">
        查询
      </ElButton>
      <ElButton size="small" text aria-label="重置筛选条件" @click="reset">重置</ElButton>
      <span class="task-filters__total num">共 {{ total }} 条任务</span>
    </div>
  </section>
</template>

<style scoped lang="scss">
.task-filters {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 12px;

  &__fields {
    display: flex;
    align-items: flex-end;
    gap: 10px;
    flex-wrap: wrap;
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 140px;

    &--wide {
      min-width: 320px;
    }
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__total {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }
}

:deep(.el-date-editor) {
  width: 100%;
}
</style>
