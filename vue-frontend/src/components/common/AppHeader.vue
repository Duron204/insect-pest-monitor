<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ElAvatar,
  ElButton,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElTooltip
} from 'element-plus'
import { useModelsStore } from '@/stores/models'
import { useUiStore } from '@/stores/ui'
import { useUserStore } from '@/stores/user'
import { confirmAction, notifySuccess } from '@/utils/notify'
import { formatTaskNo } from '@/utils'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()
const models = useModelsStore()
const user = useUserStore()

const pageTitle = computed(() => {
  const title = route.meta.title
  return typeof title === 'string' && title.length > 0 ? title : '病虫害智能监测系统'
})

const backendTone = computed<'ok' | 'warn' | 'err'>(() => {
  if (!models.backendOnline) return 'err'
  return models.modelLoaded ? 'ok' : 'warn'
})

const backendLabel = computed(() => {
  if (models.healthLoading && !models.health) return '检测后端…'
  if (!models.backendOnline) return '后端离线'
  return models.modelLoaded ? '后端在线 · 模型已加载' : '后端在线 · 未加载模型'
})

const backendTooltip = computed(() => {
  const health = models.health
  if (!health) return models.healthError ?? '无法连接 /api/health'
  const lines = [
    `版本 ${health.version}`,
    `设备 ${health.device}${health.gpu_available ? ' · GPU 可用' : ' · CPU'}`,
    `推理后端 ${health.backend}`,
    `权重 ${health.model_path ?? '未加载'}`,
    `类别 ${health.model_names ?? 0} 个`
  ]
  if (health.fallback_reason) {
    lines.push(`⚠ 后端已回退：${health.fallback_reason}`)
  }
  return lines.join('\n')
})

/** 后端回退原因（非空说明配置的后端没生效，实际用了 torch） */
const fallbackReason = computed(() => models.health?.fallback_reason ?? null)

const modelName = computed(() => models.currentName)

/**
 * 模型提示：完整路径 + 指标。
 *
 * 注意不要再把路径回退到 header 的可见文本里 —— 注册表匹配不到指标时，
 * 原先会把整条 `D:\...\weights\best-yolo11s.pt` 塞进标题栏然后被截断，
 * 既难看又没信息量。路径只放提示。
 */
const modelTooltip = computed(() => {
  const current = models.currentModel
  const lines: string[] = [models.currentPath ?? modelName.value]
  if (current) {
    lines.push(`架构 ${current.arch}`)
    if (current.map50 !== null && current.map50 !== undefined) {
      lines.push(`mAP50 ${current.map50.toFixed(4)}`)
    }
    if (current.map50_95 !== null && current.map50_95 !== undefined) {
      lines.push(`mAP50-95 ${current.map50_95.toFixed(4)}`)
    }
  }
  return lines.join('\n')
})

async function handleCommand(command: string): Promise<void> {
  if (command === 'settings') {
    await router.push({ name: 'settings' })
    return
  }
  if (command === 'theme') {
    ui.toggleTheme()
    return
  }
  if (command === 'logout') {
    const ok = await confirmAction({
      title: '退出登录',
      message: '确认退出当前账号？未保存的检测结果不会自动保留。',
      confirmText: '退出',
      danger: true
    })
    if (!ok) return
    await user.logout()
    notifySuccess('已退出登录')
    await router.replace({ name: 'login' })
  }
}

function stateClass(tone: 'ok' | 'warn' | 'err' | 'off'): string {
  return `ip-dot ip-dot--${tone}`
}
</script>

<template>
  <header class="app-header">
    <div class="app-header__left">
      <ElButton
        class="app-header__collapse"
        text
        :aria-label="ui.sidebarCollapsed ? '展开导航栏' : '收起导航栏'"
        @click="ui.toggleSidebar()"
      >
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <path
            d="M2 4h12M2 8h12M2 12h12"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
          />
        </svg>
      </ElButton>
      <div class="app-header__title-group">
        <h1 class="app-header__title">{{ pageTitle }}</h1>
      </div>
    </div>

    <div class="app-header__right">
      <!-- 后端与模型状态本来在标题下重复显示了一次，这里只保留一处，并把
           版本/设备/权重路径/回退原因都放进提示里 -->
      <ElTooltip :content="backendTooltip" placement="bottom" raw-content>
        <div class="app-header__chip" role="status" :aria-label="`后端状态：${backendLabel}`">
          <span :class="stateClass(backendTone)" aria-hidden="true" />
          <span class="app-header__chip-text">{{ backendLabel }}</span>
        </div>
      </ElTooltip>

      <!-- 推理后端回退是部署时最容易漏的问题：配置写了 onnx 却在跑 torch，
           只看配置文件发现不了。非空时必须显式告警。 -->
      <ElTooltip v-if="fallbackReason" :content="fallbackReason" placement="bottom">
        <div class="app-header__chip app-header__chip--warn" role="alert">
          <span class="ip-dot ip-dot--warn" aria-hidden="true" />
          <span class="app-header__chip-text">后端已回退</span>
        </div>
      </ElTooltip>

      <div
        class="app-header__chip"
        role="status"
        :aria-label="`实时通道状态：${ui.liveSocketLabel}`"
      >
        <span :class="stateClass(ui.liveSocketTone)" aria-hidden="true" />
        <span class="app-header__chip-text">实时通道 {{ ui.liveSocketLabel }}</span>
      </div>

      <!-- 只显示权重文件名；完整路径与指标进提示，避免标题栏被长路径撑开 -->
      <ElTooltip :content="modelTooltip" placement="bottom">
        <div class="app-header__chip app-header__chip--model" role="status">
          <span class="app-header__chip-text ip-truncate">{{ modelName }}</span>
        </div>
      </ElTooltip>

      <ElButton
        text
        :aria-label="ui.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
        @click="ui.toggleTheme()"
      >
        <svg v-if="ui.theme === 'dark'" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <circle cx="8" cy="8" r="3.2" fill="currentColor" />
          <path
            d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3.1 3.1l1.1 1.1M11.8 11.8l1.1 1.1M12.9 3.1l-1.1 1.1M4.2 11.8l-1.1 1.1"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linecap="round"
          />
        </svg>
        <svg v-else viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <path
            d="M13.5 10.2A5.8 5.8 0 0 1 5.8 2.5a5.8 5.8 0 1 0 7.7 7.7Z"
            fill="currentColor"
          />
        </svg>
      </ElButton>

      <ElDropdown trigger="click" @command="handleCommand">
        <button class="app-header__user" type="button" aria-label="打开用户菜单">
          <ElAvatar :size="26" :src="user.avatarUrl || undefined">
            {{ user.initial }}
          </ElAvatar>
          <span class="app-header__user-name ip-truncate">{{ user.displayName }}</span>
        </button>
        <template #dropdown>
          <ElDropdownMenu>
            <ElDropdownItem disabled>
              账号 ID {{ formatTaskNo(user.user?.id ?? null) }}
            </ElDropdownItem>
            <ElDropdownItem command="settings">系统设置</ElDropdownItem>
            <ElDropdownItem command="theme">
              {{ ui.theme === 'dark' ? '切换浅色主题' : '切换深色主题' }}
            </ElDropdownItem>
            <ElDropdownItem command="logout" divided>退出登录</ElDropdownItem>
          </ElDropdownMenu>
        </template>
      </ElDropdown>
    </div>
  </header>
</template>

<style scoped lang="scss">
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: var(--ip-header-h);
  padding: 0 14px;
  background: var(--ip-panel);
  border-bottom: 1px solid var(--ip-line);
  flex: 0 0 auto;

  &__left,
  &__right {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  &__collapse {
    padding: 4px;
    color: var(--ip-text-2);
  }

  &__title-group {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  &__title {
    font-size: 17px;
    font-weight: 650;
    letter-spacing: 0.01em;
    line-height: 1.3;
  }

  &__subtitle {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__chip {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 9px;
    border: 1px solid var(--ip-line);
    border-radius: 999px;
    background: var(--ip-panel-2);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
    white-space: nowrap;
  }

  &__chip-text {
    font-size: var(--ip-fs-xs);
  }

  /* 后端回退告警：用琥珀色，区别于普通状态 */
  &__chip--warn {
    border-color: var(--ip-warn-line);
    background: var(--ip-warn-weak);
    color: var(--ip-warn);
    cursor: help;
  }

  /* 当前模型：只放文件名，完整路径与指标在提示里 */
  &__chip--model {
    max-width: 190px;
    font-family: var(--ip-font-mono);
    cursor: help;

    .app-header__chip-text {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }

  &__user {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 3px 8px 3px 3px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 999px;
    color: var(--ip-text);
    cursor: pointer;
    max-width: 160px;
  }

  &__user:hover {
    border-color: var(--ip-line-strong);
    background: var(--ip-panel-2);
  }

  &__user-name {
    font-size: var(--ip-fs-sm);
    max-width: 100px;
  }
}

@media (max-width: 1279px) {
  /* 小尺寸笔记本上收起状态标签文字，只留状态灯，避免顶栏被挤爆 */
  .app-header__chip-text {
    display: none;
  }
}
</style>
