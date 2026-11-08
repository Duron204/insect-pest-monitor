<script setup lang="ts">
defineProps<{
  visible: boolean
  text?: string
}>()
</script>

<template>
  <Transition name="veil">
    <div v-if="visible" class="loading-overlay" role="status" aria-live="polite">
      <div class="loading-overlay__box">
        <span class="loading-overlay__spinner" aria-hidden="true" />
        <p class="loading-overlay__text">{{ text ?? '正在加载…' }}</p>
      </div>
    </div>
  </Transition>
</template>

<style scoped lang="scss">
.loading-overlay {
  position: fixed;
  inset: 0;
  z-index: 2900;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(6, 10, 8, 0.42);
  backdrop-filter: blur(1px);

  &__box {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 18px;
    background: var(--ip-panel);
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius);
    box-shadow: var(--ip-shadow-md);
  }

  &__spinner {
    width: 15px;
    height: 15px;
    border: 2px solid var(--ip-line-strong);
    border-top-color: var(--ip-primary);
    border-radius: 50%;
    animation: ip-spin 720ms linear infinite;
  }

  &__text {
    font-size: var(--ip-fs-sm);
    color: var(--ip-text-2);
  }
}

@keyframes ip-spin {
  to {
    transform: rotate(360deg);
  }
}

.veil-enter-active,
.veil-leave-active {
  transition: opacity var(--ip-dur) var(--ip-ease);
}

.veil-enter-from,
.veil-leave-to {
  opacity: 0;
}
</style>
