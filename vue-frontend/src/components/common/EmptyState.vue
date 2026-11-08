<script setup lang="ts">
withDefaults(
  defineProps<{
    description?: string
    hint?: string
    actionText?: string
    compact?: boolean
  }>(),
  {
    description: '暂无数据',
    hint: '',
    actionText: '',
    compact: false
  }
)

const emit = defineEmits<{ (event: 'action'): void }>()
</script>

<template>
  <div class="empty-state" :class="{ 'empty-state--compact': compact }">
    <svg class="empty-state__mark" viewBox="0 0 48 40" aria-hidden="true">
      <path
        d="M4 30 L14 12 L24 26 L32 14 L44 30"
        fill="none"
        stroke="var(--ip-line-strong)"
        stroke-width="1.6"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <path d="M4 34 H44" stroke="var(--ip-line)" stroke-width="1.6" stroke-linecap="round" />
      <circle cx="34" cy="10" r="3" fill="none" stroke="var(--ip-line-strong)" stroke-width="1.4" />
    </svg>
    <p class="empty-state__title">{{ description }}</p>
    <p v-if="hint" class="empty-state__hint">{{ hint }}</p>
    <button
      v-if="actionText"
      class="empty-state__action"
      type="button"
      :aria-label="actionText"
      @click="emit('action')"
    >
      {{ actionText }}
    </button>
  </div>
</template>

<style scoped lang="scss">
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 32px 16px;
  min-height: 240px;
  text-align: center;

  &--compact {
    min-height: 150px;
    padding: 16px 12px;
  }

  &__mark {
    width: 74px;
    height: 62px;
    opacity: 0.9;
  }

  &__title {
    margin-top: 6px;
    font-size: var(--ip-fs-body);
    font-weight: 600;
    color: var(--ip-text-2);
  }

  &__hint {
    margin-top: 2px;
    font-size: var(--ip-fs-sm);
    line-height: 1.6;
    color: var(--ip-text-3);
    max-width: 380px;
  }

  /* 主行动按钮用实心样式：空状态里它是唯一的下一步，弱化它等于让人停在原地 */
  &__action {
    margin-top: 12px;
    padding: 8px 22px;
    font-size: var(--ip-fs-body);
    font-weight: 600;
    border-radius: var(--ip-radius);
    border: 1px solid var(--ip-primary);
    background: var(--ip-primary);
    color: var(--ip-text-inv);
    cursor: pointer;
    transition: background var(--ip-dur-fast) var(--ip-ease),
      transform var(--ip-dur-fast) var(--ip-ease);

    &:hover {
      background: var(--ip-primary-hover);
      transform: translateY(-1px);
    }
  }
}
</style>
