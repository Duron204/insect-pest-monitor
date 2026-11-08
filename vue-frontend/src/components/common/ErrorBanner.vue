<script setup lang="ts">
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { useErrorStore } from '@/stores/error'

const props = withDefaults(
  defineProps<{
    /** 覆盖 store 中的消息（页面局部使用） */
    message?: string | null
    detail?: string | null
    title?: string
    closable?: boolean
  }>(),
  {
    message: null,
    detail: null,
    title: '请求失败',
    closable: true
  }
)

const errorStore = useErrorStore()

const emit = defineEmits<{ (event: 'close'): void }>()

const text = computed(() => props.message ?? errorStore.message)
const extra = computed(() => props.detail ?? errorStore.detail)

function close(): void {
  if (props.message === null) errorStore.clear()
  else emit('close')
}

function copyDetail(): void {
  const value = extra.value
  if (!value) return
  void navigator.clipboard
    .writeText(value)
    .then(() => ElMessage({ type: 'success', message: '错误详情已复制', duration: 2000 }))
    .catch(() => ElMessage({ type: 'warning', message: '复制失败，请手动选择文本', duration: 2400 }))
}
</script>

<template>
  <Transition name="banner">
    <div v-if="text" class="error-banner" role="alert">
      <span class="error-banner__icon" aria-hidden="true">!</span>
      <div class="error-banner__body">
        <p class="error-banner__title">{{ title }}</p>
        <p class="error-banner__text">{{ text }}</p>
        <p v-if="extra" class="error-banner__detail">{{ extra }}</p>
      </div>
      <button
        v-if="extra"
        class="error-banner__copy"
        type="button"
        aria-label="复制错误详情"
        @click="copyDetail"
      >
        复制详情
      </button>
      <button
        v-if="closable"
        class="error-banner__close"
        type="button"
        aria-label="关闭错误提示"
        @click="close"
      >
        ×
      </button>
    </div>
  </Transition>
</template>

<style scoped lang="scss">
.error-banner {
  position: fixed;
  left: 50%;
  bottom: 22px;
  transform: translateX(-50%);
  z-index: 3000;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  max-width: min(680px, calc(100vw - 32px));
  padding: 12px 14px;
  background: var(--ip-panel);
  border: 1px solid var(--ip-danger);
  border-radius: var(--ip-radius);
  box-shadow: var(--ip-shadow-md);

  &__icon {
    flex: 0 0 auto;
    width: 18px;
    height: 18px;
    margin-top: 1px;
    border-radius: 50%;
    background: var(--ip-danger);
    color: var(--ip-text-inv);
    font-size: var(--ip-fs-xs);
    font-weight: 700;
    line-height: 18px;
    text-align: center;
  }

  &__body {
    min-width: 0;
  }

  &__title {
    font-size: var(--ip-fs-xs);
    letter-spacing: 0.04em;
    color: var(--ip-danger);
    font-weight: 600;
  }

  &__text {
    font-size: var(--ip-fs-sm);
    color: var(--ip-text);
    word-break: break-word;
  }

  &__detail {
    margin-top: 4px;
    font-family: var(--ip-font-mono);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    word-break: break-all;
  }

  &__copy,
  &__close {
    flex: 0 0 auto;
    background: transparent;
    border: 1px solid var(--ip-line);
    color: var(--ip-text-2);
    border-radius: var(--ip-radius-sm);
    font-size: var(--ip-fs-xs);
    padding: 2px 8px;
    cursor: pointer;
  }

  &__close {
    border: none;
    font-size: 18px;
    line-height: 1;
    padding: 0 2px;
  }

  &__copy:hover,
  &__close:hover {
    color: var(--ip-text);
    border-color: var(--ip-line-strong);
  }
}

.banner-enter-active,
.banner-leave-active {
  transition: opacity var(--ip-dur) var(--ip-ease), transform var(--ip-dur) var(--ip-ease);
}

.banner-enter-from,
.banner-leave-to {
  opacity: 0;
  transform: translate(-50%, 8px);
}
</style>
