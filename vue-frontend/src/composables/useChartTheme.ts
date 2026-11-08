import { computed, ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'

/**
 * 图表配色：从 CSS 变量读取，主题切换后自动重算。
 * 不引入任何 UI 库之外的依赖，也避免硬编码色值。
 */

const FALLBACK = {
  primary: '#2f9e5f',
  text: '#e6efe8',
  text2: '#a9bdb0',
  text3: '#7b9083',
  line: '#26372c',
  panel: '#16211b',
  warn: '#e8a33d',
  info: '#4a90d9',
  danger: '#d9534f'
}

export interface ChartPalette {
  primary: string
  text: string
  text2: string
  text3: string
  line: string
  panel: string
  warn: string
  info: string
  danger: string
  series: string[]
}

function readVar(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value.length > 0 ? value : fallback
}

export function useChartTheme() {
  const ui = useUiStore()
  const palette = ref<ChartPalette>(buildPalette())

  function buildPalette(): ChartPalette {
    return {
      primary: readVar('--ip-primary', FALLBACK.primary),
      text: readVar('--ip-text', FALLBACK.text),
      text2: readVar('--ip-text-2', FALLBACK.text2),
      text3: readVar('--ip-text-3', FALLBACK.text3),
      line: readVar('--ip-line', FALLBACK.line),
      panel: readVar('--ip-panel', FALLBACK.panel),
      warn: readVar('--ip-warn', FALLBACK.warn),
      info: readVar('--ip-info', FALLBACK.info),
      danger: readVar('--ip-danger', FALLBACK.danger),
      series: [
        readVar('--ip-primary', FALLBACK.primary),
        '#4fc07e',
        readVar('--ip-warn', FALLBACK.warn),
        '#4a90d9',
        '#8e6fd0',
        '#c0556f',
        '#3fb0a5',
        '#9bb43a'
      ]
    }
  }

  function refresh(): void {
    palette.value = buildPalette()
  }

  watch(
    () => ui.theme,
    () => {
      // 等 data-theme 生效后再读变量
      if (typeof window === 'undefined') {
        refresh()
        return
      }
      window.requestAnimationFrame(refresh)
    }
  )

  const seriesColor = computed(() => palette.value.series)

  function colorAt(index: number): string {
    const list = palette.value.series
    if (list.length === 0) return FALLBACK.primary
    const safe = ((index % list.length) + list.length) % list.length
    return list[safe] ?? FALLBACK.primary
  }

  return { palette, refresh, seriesColor, colorAt }
}
