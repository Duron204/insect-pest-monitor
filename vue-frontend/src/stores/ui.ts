import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { readString, writeString } from '@/utils/storage'
import type { DetectionSocketState } from '@/types'

export type ThemeName = 'dark' | 'light'

const THEME_KEY = 'ip_theme'
const SIDEBAR_KEY = 'ip_sidebar_collapsed'

export const useUiStore = defineStore('ui', () => {
  /* ------------------------------- 主题 ------------------------------- */

  const theme = ref<ThemeName>(readString(THEME_KEY, 'dark') === 'light' ? 'light' : 'dark')

  function applyTheme(value: ThemeName): void {
    if (typeof document === 'undefined') return
    document.documentElement.setAttribute('data-theme', value)
  }

  function setTheme(value: ThemeName): void {
    theme.value = value
    writeString(THEME_KEY, value)
    applyTheme(value)
  }

  function toggleTheme(): void {
    setTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  /** 在 main.ts 挂载前调用，避免首屏闪烁 */
  function initTheme(): void {
    applyTheme(theme.value)
  }

  watch(theme, (value) => applyTheme(value))

  /* ------------------------------- 侧栏 ------------------------------- */

  const sidebarCollapsed = ref(readString(SIDEBAR_KEY, '0') === '1')

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
    writeString(SIDEBAR_KEY, sidebarCollapsed.value ? '1' : '0')
  }

  function setSidebarCollapsed(value: boolean): void {
    sidebarCollapsed.value = value
    writeString(SIDEBAR_KEY, value ? '1' : '0')
  }

  /* ------------------------------ 全局加载 ------------------------------ */

  const globalLoading = ref(false)
  const globalLoadingText = ref('正在加载…')

  function setGlobalLoading(value: boolean, text = '正在加载…'): void {
    globalLoading.value = value
    globalLoadingText.value = text
  }

  /* --------------------------- 实时通道状态 --------------------------- */

  /** 由检测台的 useDetectionSocket 写入，顶部栏据此显示真实连接状态灯 */
  const liveSocketState = ref<DetectionSocketState>('idle')

  function setLiveSocketState(value: DetectionSocketState): void {
    liveSocketState.value = value
  }

  const liveSocketLabel = computed(() => {
    switch (liveSocketState.value) {
      case 'idle':
        return '未启用'
      case 'connecting':
        return '连接中'
      case 'open':
        return '握手中'
      case 'ready':
        return '实时在线'
      case 'reconnecting':
        return '重连中'
      case 'closed':
        return '已断开'
      case 'error':
        return '连接异常'
      default:
        return '未知'
    }
  })

  const liveSocketTone = computed<'ok' | 'warn' | 'err' | 'off'>(() => {
    switch (liveSocketState.value) {
      case 'ready':
        return 'ok'
      case 'connecting':
      case 'open':
      case 'reconnecting':
        return 'warn'
      case 'error':
        return 'err'
      default:
        return 'off'
    }
  })

  return {
    theme,
    setTheme,
    toggleTheme,
    initTheme,
    sidebarCollapsed,
    toggleSidebar,
    setSidebarCollapsed,
    globalLoading,
    globalLoadingText,
    setGlobalLoading,
    liveSocketState,
    setLiveSocketState,
    liveSocketLabel,
    liveSocketTone
  }
})
