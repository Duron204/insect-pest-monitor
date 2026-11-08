import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 页面内错误横幅（用于替代 alert 与难以察觉的 toast） */
export const useErrorStore = defineStore('error', () => {
  const message = ref<string | null>(null)
  const detail = ref<string | null>(null)

  function show(text: string, extra?: string | null): void {
    message.value = text
    detail.value = extra ?? null
  }

  function clear(): void {
    message.value = null
    detail.value = null
  }

  return { message, detail, show, clear }
})
