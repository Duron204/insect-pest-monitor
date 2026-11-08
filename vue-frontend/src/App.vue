<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import { RouterView } from 'vue-router'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import LoadingOverlay from '@/components/common/LoadingOverlay.vue'
import { useAlertsStore } from '@/stores/alerts'
import { useModelsStore } from '@/stores/models'
import { useUiStore } from '@/stores/ui'
import { useUserStore } from '@/stores/user'

const ui = useUiStore()
const models = useModelsStore()
const alerts = useAlertsStore()
const user = useUserStore()

/** 未确认告警数的刷新间隔：告警不需要秒级实时，1 分钟足够且不会压后端 */
const ALERT_POLL_MS = 60_000
let alertTimer: ReturnType<typeof setInterval> | null = null

function startAlertPolling(): void {
  void alerts.refresh()
  if (alertTimer === null) {
    alertTimer = setInterval(() => void alerts.refresh(), ALERT_POLL_MS)
  }
}

function stopAlertPolling(): void {
  if (alertTimer !== null) {
    clearInterval(alertTimer)
    alertTimer = null
  }
  alerts.setUnacked(0)
}

/**
 * 用 watch 而不是 onMounted：App 只在首次加载时挂载一次，
 * 而登录发生在挂载之后 —— 只写 onMounted 的话，登录后侧栏红点不会开始轮询。
 */
watch(
  () => user.isAuthenticated,
  async (authed) => {
    if (authed) {
      models.startHealthPolling()
      startAlertPolling()
      // 用已存 token 静默刷新一次用户信息（token 失效会被拦截器处理并登出）
      try {
        await user.fetchMe()
      } catch {
        // 有意留空：拦截器已经处理了未授权，这里不该再抛到控制台
      }
    } else {
      models.stopHealthPolling()
      stopAlertPolling()
    }
  },
  { immediate: true }
)

onBeforeUnmount(() => {
  models.stopHealthPolling()
  stopAlertPolling()
})
</script>

<template>
  <div class="app-root">
    <RouterView />
    <ErrorBanner />
    <LoadingOverlay :visible="ui.globalLoading" :text="ui.globalLoadingText" />
  </div>
</template>

<style scoped lang="scss">
.app-root {
  min-height: 100%;
  background: var(--ip-bg);
  color: var(--ip-text);
}
</style>
