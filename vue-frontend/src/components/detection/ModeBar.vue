<script setup lang="ts">
import type { DetectMode } from '@/stores/session'

const props = defineProps<{
  modelValue: DetectMode
  disabled?: boolean
  /** 各模式的可用性由后端 health / 浏览器能力驱动，不用假状态 */
  cameraSupported?: boolean
}>()

const emit = defineEmits<{
  (event: 'update:modelValue', value: DetectMode): void
  (event: 'pick-image'): void
  (event: 'pick-folder'): void
  (event: 'pick-video'): void
  (event: 'camera'): void
}>()

interface ModeEntry {
  mode: DetectMode
  title: string
  hint: string
  action: 'pick-image' | 'pick-folder' | 'pick-video' | 'camera'
}

const MODES: ModeEntry[] = [
  { mode: 'image', title: '选择图片', hint: '单张图像检测', action: 'pick-image' },
  { mode: 'batch', title: '选择文件夹', hint: '多图批量（≤50）', action: 'pick-folder' },
  { mode: 'video', title: '选择视频', hint: '后台逐帧检测', action: 'pick-video' },
  { mode: 'camera', title: '开启摄像头', hint: '实时流检测', action: 'camera' }
]

function pick(entry: ModeEntry): void {
  if (props.disabled) return
  emit('update:modelValue', entry.mode)
  switch (entry.action) {
    case 'pick-image':
      emit('pick-image')
      break
    case 'pick-folder':
      emit('pick-folder')
      break
    case 'pick-video':
      emit('pick-video')
      break
    case 'camera':
      emit('camera')
      break
    default:
      break
  }
}

function isDisabled(entry: ModeEntry): boolean {
  if (props.disabled) return true
  if (entry.mode === 'camera') return props.cameraSupported === false
  return false
}
</script>

<template>
  <div class="mode-bar" role="group" aria-label="检测模式">
    <button
      v-for="entry in MODES"
      :key="entry.mode"
      class="mode-bar__item"
      :class="{ 'is-active': entry.mode === props.modelValue }"
      type="button"
      :disabled="isDisabled(entry)"
      :aria-label="`${entry.title}（${entry.hint}）`"
      :aria-pressed="entry.mode === props.modelValue"
      @click="pick(entry)"
    >
      <span class="mode-bar__icon" aria-hidden="true">
        <svg v-if="entry.mode === 'image'" viewBox="0 0 20 20" width="18" height="18">
          <rect x="2.6" y="4.2" width="14.8" height="11.6" rx="2" fill="none" stroke="currentColor" stroke-width="1.5" />
          <circle cx="7.2" cy="8" r="1.5" fill="currentColor" />
          <path d="M3.4 14.4 8 10.2l3.4 3 2.2-2 2.8 2.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
        </svg>
        <svg v-else-if="entry.mode === 'batch'" viewBox="0 0 20 20" width="18" height="18">
          <path
            d="M2.6 6.4a1.4 1.4 0 0 1 1.4-1.4h3.2l1.4 1.8h6.4a1.4 1.4 0 0 1 1.4 1.4v6.4a1.4 1.4 0 0 1-1.4 1.4H4a1.4 1.4 0 0 1-1.4-1.4z"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linejoin="round"
          />
          <path d="M6.6 13.2h6.8M6.6 10.6h4.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
        </svg>
        <svg v-else-if="entry.mode === 'video'" viewBox="0 0 20 20" width="18" height="18">
          <rect x="2.4" y="4.6" width="11.4" height="10.8" rx="2" fill="none" stroke="currentColor" stroke-width="1.5" />
          <path d="M13.8 9.6 18 7.2v5.6l-4.2-2.4z" fill="currentColor" />
        </svg>
        <svg v-else viewBox="0 0 20 20" width="18" height="18">
          <circle cx="10" cy="10" r="6.6" fill="none" stroke="currentColor" stroke-width="1.5" />
          <circle cx="10" cy="10" r="2.4" fill="none" stroke="currentColor" stroke-width="1.5" />
          <path d="M3.6 3.6 6 6M16.4 3.6 14 6M3.6 16.4 6 14M16.4 16.4 14 14" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
        </svg>
      </span>
      <span class="mode-bar__text">
        <span class="mode-bar__title">{{ entry.title }}</span>
        <span class="mode-bar__hint">{{ entry.hint }}</span>
      </span>
    </button>
    <p v-if="cameraSupported === false" class="mode-bar__warn">
      当前浏览器不支持摄像头采集（需 HTTPS 或 localhost）
    </p>
  </div>
</template>

<style scoped lang="scss">
.mode-bar {
  display: flex;
  flex-direction: column;
  gap: 6px;

  &__item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 10px;
    background: var(--ip-panel-2);
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
    color: var(--ip-text-2);
    text-align: left;
    cursor: pointer;
    transition: border-color var(--ip-dur-fast) var(--ip-ease),
      background var(--ip-dur-fast) var(--ip-ease), color var(--ip-dur-fast) var(--ip-ease);

    &:hover:not(:disabled) {
      border-color: var(--ip-primary-line);
      color: var(--ip-text);
      background: var(--ip-primary-weak);
    }

    &.is-active {
      border-color: var(--ip-primary);
      background: var(--ip-primary-weak);
      color: var(--ip-text);
    }

    &:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
  }

  &__icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    flex: 0 0 auto;
  }

  &__text {
    display: flex;
    flex-direction: column;
    min-width: 0;
    line-height: 1.25;
  }

  &__title {
    font-size: var(--ip-fs-sm);
  }

  &__hint {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__warn {
    font-size: var(--ip-fs-xs);
    color: var(--ip-warn);
    line-height: 1.5;
  }
}
</style>
