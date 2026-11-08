import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { listModels, selectModel as selectModelApi, uploadModel as uploadModelApi } from '@/api/models'
import { health as healthApi } from '@/api/system'
import { notifyApiError, notifySuccess } from '@/utils/notify'
import type { HealthOut, ModelOut } from '@/types'

const HEALTH_POLL_MS = 30_000

export const useModelsStore = defineStore('models', () => {
  /* ---------------------------- 模型注册表 ---------------------------- */

  const items = ref<ModelOut[]>([])
  const currentPath = ref<string | null>(null)
  const recommendedId = ref<number | null>(null)
  const loading = ref(false)
  const switching = ref(false)
  const error = ref<string | null>(null)
  const loaded = ref(false)

  const recommended = computed<ModelOut | null>(() => {
    if (recommendedId.value === null) return null
    return items.value.find((item) => item.id === recommendedId.value) ?? null
  })

  const currentModel = computed<ModelOut | null>(() => {
    if (!currentPath.value) return null
    const byPath = items.value.find((item) => item.path === currentPath.value)
    if (byPath) return byPath
    const name = currentPath.value.replace(/\\/g, '/').split('/').pop() ?? ''
    return items.value.find((item) => item.name === name) ?? null
  })

  const currentName = computed(() => {
    if (currentModel.value) return currentModel.value.name
    if (!currentPath.value) return '未加载'
    const parts = currentPath.value.replace(/\\/g, '/').split('/')
    return parts[parts.length - 1] ?? currentPath.value
  })

  const currentModelId = computed(() => currentModel.value?.id ?? null)

  async function loadRegistry(force = false): Promise<void> {
    if (loading.value) return
    if (loaded.value && !force) return
    loading.value = true
    error.value = null
    try {
      const registry = await listModels()
      items.value = registry.items ?? []
      currentPath.value = registry.current
      recommendedId.value = registry.recommended_id
      loaded.value = true
    } catch (err) {
      error.value = notifyApiError(err)
    } finally {
      loading.value = false
    }
  }

  async function selectModel(id: number): Promise<ModelOut | null> {
    switching.value = true
    error.value = null
    try {
      const model = await selectModelApi(id)
      currentPath.value = model.path
      const index = items.value.findIndex((item) => item.id === model.id)
      if (index >= 0) items.value.splice(index, 1, model)
      else items.value.unshift(model)
      notifySuccess(`已切换到模型「${model.name}」`)
      return model
    } catch (err) {
      error.value = notifyApiError(err)
      return null
    } finally {
      switching.value = false
    }
  }

  async function uploadModel(file: File, onProgress?: (ratio: number) => void): Promise<ModelOut | null> {
    switching.value = true
    error.value = null
    try {
      const model = await uploadModelApi(file, { onProgress })
      notifySuccess(`模型「${model.name}」已注册`)
      await loadRegistry(true)
      return model
    } catch (err) {
      error.value = notifyApiError(err)
      return null
    } finally {
      switching.value = false
    }
  }

  function isCurrent(model: ModelOut): boolean {
    if (currentPath.value && model.path === currentPath.value) return true
    return currentModel.value?.id === model.id
  }

  function isRecommended(model: ModelOut): boolean {
    return recommendedId.value !== null && model.id === recommendedId.value
  }

  /** 精度排序：mAP50-95 优先，其次 mAP50 */
  const ranked = computed(() =>
    [...items.value].sort((a, b) => {
      const left = a.map50_95 ?? a.map50 ?? -1
      const right = b.map50_95 ?? b.map50 ?? -1
      return right - left
    })
  )

  /* ------------------------------ 后端健康 ------------------------------ */

  const health = ref<HealthOut | null>(null)
  const healthLoading = ref(false)
  const healthError = ref<string | null>(null)
  const lastHealthAt = ref<number | null>(null)

  let pollTimer: number | null = null

  async function loadHealth(): Promise<HealthOut | null> {
    healthLoading.value = true
    try {
      const result = await healthApi()
      health.value = result
      healthError.value = null
      lastHealthAt.value = Date.now()
      if (result.model_loaded && result.model_path) currentPath.value = result.model_path
      return result
    } catch (err) {
      health.value = null
      healthError.value = (err as { message?: string })?.message ?? '无法连接后端'
      return null
    } finally {
      healthLoading.value = false
    }
  }

  function startHealthPolling(): void {
    if (typeof window === 'undefined') return
    if (pollTimer !== null) return
    void loadHealth()
    pollTimer = window.setInterval(() => {
      void loadHealth()
    }, HEALTH_POLL_MS)
  }

  function stopHealthPolling(): void {
    if (pollTimer === null || typeof window === 'undefined') return
    window.clearInterval(pollTimer)
    pollTimer = null
  }

  const backendOnline = computed(() => health.value !== null)
  const modelLoaded = computed(() => health.value?.model_loaded === true)
  // health.model_names 是**类别数**（数字），不是类别名数组。
  // 之前写成 ?.length，数字没有 length，于是页面上永远显示 0。
  const classCount = computed(() => health.value?.model_names ?? 0)

  function reset(): void {
    items.value = []
    currentPath.value = null
    recommendedId.value = null
    loaded.value = false
    error.value = null
  }

  return {
    items,
    currentPath,
    recommendedId,
    recommended,
    currentModel,
    currentModelId,
    currentName,
    ranked,
    loading,
    switching,
    error,
    loaded,
    loadRegistry,
    selectModel,
    uploadModel,
    isCurrent,
    isRecommended,
    health,
    healthLoading,
    healthError,
    lastHealthAt,
    loadHealth,
    startHealthPolling,
    stopHealthPolling,
    backendOnline,
    modelLoaded,
    classCount,
    reset
  }
})
