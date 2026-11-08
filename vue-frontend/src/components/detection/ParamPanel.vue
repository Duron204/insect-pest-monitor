<script setup lang="ts">
import { computed } from 'vue'
import { ElButton, ElInputNumber, ElSlider } from 'element-plus'

const props = withDefaults(
  defineProps<{
    conf: number
    iou: number
    stride: number
    targetFps: number
    showStride?: boolean
    showFps?: boolean
    /** 实时通道已连上时，滑块改动会立刻下发 update_params */
    liveConnected?: boolean
    disabled?: boolean
  }>(),
  {
    showStride: false,
    showFps: false,
    liveConnected: false,
    disabled: false
  }
)

const emit = defineEmits<{
  (event: 'update:conf', value: number): void
  (event: 'update:iou', value: number): void
  (event: 'update:stride', value: number): void
  (event: 'update:targetFps', value: number): void
  (event: 'reset'): void
}>()

const CONFIG_PRESETS: { label: string; conf: number; iou: number; hint: string }[] = [
  { label: '宽松', conf: 0.15, iou: 0.5, hint: '尽量不漏检，误检可能偏多' },
  { label: '均衡', conf: 0.25, iou: 0.45, hint: '默认，适合大多数田间图像' },
  { label: '严格', conf: 0.5, iou: 0.4, hint: '只看高置信目标' }
]

const liveHint = computed(() =>
  props.liveConnected ? '改动立即生效（已下发到实时通道）' : '改动将随下一次检测请求发送'
)

function onConf(value: number | number[]): void {
  if (typeof value === 'number') emit('update:conf', value)
}

function onIou(value: number | number[]): void {
  if (typeof value === 'number') emit('update:iou', value)
}

function applyPreset(conf: number, iou: number): void {
  emit('update:conf', conf)
  emit('update:iou', iou)
}
</script>

<template>
  <section class="param-panel ip-panel" aria-label="检测参数">
    <header class="param-panel__head">
      <h2 class="ip-section-title">检测参数</h2>
      <ElButton text size="small" :aria-label="'恢复默认参数'" @click="emit('reset')">
        恢复默认
      </ElButton>
    </header>

    <div class="param-panel__field">
      <div class="param-panel__label-row">
        <label class="ip-label" for="param-conf">置信度阈值 conf</label>
        <span class="param-panel__value num">{{ conf.toFixed(2) }}</span>
      </div>
      <ElSlider
        id="param-conf"
        :model-value="conf"
        :min="0"
        :max="0.99"
        :step="0.01"
        :disabled="disabled"
        :show-tooltip="false"
        aria-label="置信度阈值"
        @update:model-value="onConf"
      />
      <p class="param-panel__hint">低于该置信度的目标会被丢弃</p>
    </div>

    <div class="param-panel__field">
      <div class="param-panel__label-row">
        <label class="ip-label" for="param-iou">重叠度阈值 IoU</label>
        <span class="param-panel__value num">{{ iou.toFixed(2) }}</span>
      </div>
      <ElSlider
        id="param-iou"
        :model-value="iou"
        :min="0"
        :max="0.99"
        :step="0.01"
        :disabled="disabled"
        :show-tooltip="false"
        aria-label="重叠度阈值"
        @update:model-value="onIou"
      />
      <p class="param-panel__hint">非极大值抑制的合并阈值</p>
    </div>

    <div class="param-panel__presets">
      <button
        v-for="preset in CONFIG_PRESETS"
        :key="preset.label"
        class="param-panel__preset"
        type="button"
        :title="preset.hint"
        :aria-label="`${preset.label}：conf ${preset.conf}，IoU ${preset.iou}`"
        :disabled="disabled"
        @click="applyPreset(preset.conf, preset.iou)"
      >
        {{ preset.label }}
      </button>
    </div>

    <div v-if="showStride" class="param-panel__field">
      <div class="param-panel__label-row">
        <label class="ip-label" for="param-stride">视频抽帧步长 stride</label>
      </div>
      <ElInputNumber
        id="param-stride"
        :model-value="stride"
        :min="1"
        :max="20"
        :step="1"
        size="small"
        controls-position="right"
        aria-label="视频抽帧步长"
        @update:model-value="(value: number | undefined) => emit('update:stride', value ?? 1)"
      />
      <p class="param-panel__hint">每 N 帧取 1 帧推理，越大越快、越可能漏帧</p>
    </div>

    <div v-if="showFps" class="param-panel__field">
      <div class="param-panel__label-row">
        <label class="ip-label" for="param-fps">采集帧率</label>
        <span class="param-panel__value num">{{ targetFps }} fps</span>
      </div>
      <ElSlider
        id="param-fps"
        :model-value="targetFps"
        :min="1"
        :max="30"
        :step="1"
        :show-tooltip="false"
        aria-label="摄像头采集帧率"
        @update:model-value="(value: number | number[]) => typeof value === 'number' && emit('update:targetFps', value)"
      />
      <p class="param-panel__hint">单实例设计，帧率过高会积压</p>
    </div>

    <p class="param-panel__live" :class="{ 'is-live': liveConnected }">
      <span class="ip-dot" :class="liveConnected ? 'ip-dot--ok' : 'ip-dot--off'" aria-hidden="true" />
      {{ liveHint }}
    </p>
  </section>
</template>

<style scoped lang="scss">
.param-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__label-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__value {
    font-size: var(--ip-fs-sm);
    color: var(--ip-primary-hover);
  }

  &__hint {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    line-height: 1.4;
  }

  &__presets {
    display: flex;
    gap: 6px;
  }

  &__preset {
    flex: 1 1 0;
    padding: 4px 0;
    font-size: var(--ip-fs-xs);
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
    background: transparent;
    color: var(--ip-text-2);
    cursor: pointer;

    &:hover:not(:disabled) {
      border-color: var(--ip-primary-line);
      color: var(--ip-text);
      background: var(--ip-primary-weak);
    }

    &:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
  }

  &__live {
    display: flex;
    align-items: center;
    gap: 6px;
    padding-top: 8px;
    border-top: 1px solid var(--ip-line-soft);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);

    &.is-live {
      color: var(--ip-primary-hover);
    }
  }
}

:deep(.el-input-number) {
  width: 100%;
}
</style>
