<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAlertsStore } from '@/stores/alerts'
import { useUiStore } from '@/stores/ui'

interface NavItem {
  name: string
  title: string
  icon: string
  hint: string
}

/** 导航项与 router 中已实现的页面一一对应（地块管理仍未做界面，故不放死链）。 */
const NAV_ITEMS: NavItem[] = [
  { name: 'detect', title: '检测台', icon: 'Aim', hint: '单图 / 批量 / 视频 / 摄像头' },
  { name: 'assistant', title: 'AI 助手', icon: 'Spark', hint: '知识库检索 + 实时虫情问答' },
  { name: 'history', title: '历史记录', icon: 'Files', hint: '任务列表与报告导出' },
  { name: 'dashboard', title: '统计看板', icon: 'DataLine', hint: '类别分布与趋势' },
  { name: 'alerts', title: '告警中心', icon: 'Bell', hint: '阈值告警与确认' },
  { name: 'models', title: '模型管理', icon: 'Cpu', hint: '注册表与切换' },
  { name: 'species', title: '虫害知识库', icon: 'Notebook', hint: '15 类害虫档案' },
  // 与下面那条「设备状态」成对：一个是田里的盒子，一个是跑后端这台机器
  { name: 'devices', title: '我的设备', icon: 'Devices', hint: '绑定树莓派 · 实时画面 · 检测结果' },
  { name: 'device', title: '设备状态', icon: 'Monitor', hint: '主机健康与最近抓拍' },
  { name: 'settings', title: '系统设置', icon: 'Setting', hint: '账号与后端信息' }
]

const ui = useUiStore()
const alerts = useAlertsStore()
const route = useRoute()
const router = useRouter()

const activeName = computed(() => (typeof route.name === 'string' ? route.name : ''))

function go(name: string): void {
  if (activeName.value === name) return
  void router.push({ name })
}
</script>

<template>
  <!-- 左侧图标+文字导航（原先窄屏会换成底部导航，已按"只在电脑上用"移除） -->
  <nav
    class="app-sidebar"
    :class="{ 'app-sidebar--collapsed': ui.sidebarCollapsed }"
    aria-label="主导航"
  >
    <div class="app-sidebar__brand">
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
        <path
          d="M12 21c0-5 1.5-9 6-12M12 21c0-5-1.5-9-6-12M12 21v-6"
          fill="none"
          stroke="var(--ip-primary)"
          stroke-width="1.6"
          stroke-linecap="round"
        />
        <path
          d="M12 9c2.6 0 4.6-1.6 5.4-4.2C14.6 4 12.6 5.6 12 8.2M12 9C9.4 9 7.4 7.4 6.6 4.8 9.4 4 11.4 5.6 12 8.2"
          fill="none"
          stroke="var(--ip-primary)"
          stroke-width="1.4"
          stroke-linecap="round"
        />
      </svg>
      <span v-if="!ui.sidebarCollapsed" class="app-sidebar__brand-text">
        <strong>病虫害监测</strong>
        <small>Insect Pest Monitor</small>
      </span>
    </div>

    <ul class="app-sidebar__list">
      <li v-for="item in NAV_ITEMS" :key="item.name">
        <button
          class="app-sidebar__item"
          :class="{ 'is-active': activeName === item.name }"
          type="button"
          :aria-label="item.title"
          :aria-current="activeName === item.name ? 'page' : undefined"
          :title="ui.sidebarCollapsed ? `${item.title} · ${item.hint}` : item.hint"
          @click="go(item.name)"
        >
          <span class="app-sidebar__icon" aria-hidden="true">
            <svg v-if="item.icon === 'Aim'" viewBox="0 0 20 20" width="17" height="17">
              <circle cx="10" cy="10" r="6.4" fill="none" stroke="currentColor" stroke-width="1.5" />
              <circle cx="10" cy="10" r="1.6" fill="currentColor" />
              <path d="M10 1.6v3M10 15.4v3M1.6 10h3M15.4 10h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            <svg v-else-if="item.icon === 'Files'" viewBox="0 0 20 20" width="17" height="17">
              <path
                d="M4 3.4h6.2L15 8.2V16.6H4z"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linejoin="round"
              />
              <path d="M10 3.6V8.4h4.8M6.6 11.6h6.4M6.6 14h4.2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
            </svg>
            <svg v-else-if="item.icon === 'DataLine'" viewBox="0 0 20 20" width="17" height="17">
              <path d="M3 16.4h14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              <path
                d="M4.6 13.2 8 8.8l3 2.6 4.4-6"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
            <svg v-else-if="item.icon === 'Cpu'" viewBox="0 0 20 20" width="17" height="17">
              <rect x="5" y="5" width="10" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="1.5" />
              <rect x="8.2" y="8.2" width="3.6" height="3.6" rx="1" fill="currentColor" />
              <path
                d="M8 2.4v2.2M12 2.4v2.2M8 15.4v2.2M12 15.4v2.2M2.4 8h2.2M2.4 12h2.2M15.4 8h2.2M15.4 12h2.2"
                stroke="currentColor"
                stroke-width="1.4"
                stroke-linecap="round"
              />
            </svg>
            <svg v-else-if="item.icon === 'Spark'" viewBox="0 0 20 20" width="17" height="17">
              <path
                d="M10 2.6l1.3 3.4 3.4 1.3-3.4 1.3L10 12l-1.3-3.4L5.3 7.3l3.4-1.3z"
                fill="none"
                stroke="currentColor"
                stroke-width="1.4"
                stroke-linejoin="round"
              />
              <path d="M14.6 12.4l.7 1.7 1.7.7-1.7.7-.7 1.7-.7-1.7-1.7-.7 1.7-.7z" fill="currentColor" opacity="0.7" />
            </svg>
            <svg v-else-if="item.icon === 'Notebook'" viewBox="0 0 20 20" width="17" height="17">
              <path
                d="M5 3.4h10v13.2H5z"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linejoin="round"
              />
              <path d="M7.6 3.4v13.2M3.4 6.8h1.6M3.4 10h1.6M3.4 13.2h1.6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
            </svg>
            <svg v-else-if="item.icon === 'Bell'" viewBox="0 0 20 20" width="17" height="17">
              <path
                d="M10 2.8a4.6 4.6 0 0 0-4.6 4.6v3.2L4 13.4h12l-1.4-2.8V7.4A4.6 4.6 0 0 0 10 2.8Z"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linejoin="round"
              />
              <path d="M8 15.6a2 2 0 0 0 4 0" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
            <!-- 显示器 + 内部折线：这一页看的是"跑后端那台机器"的健康与产出 -->
            <svg v-else-if="item.icon === 'Monitor'" viewBox="0 0 20 20" width="17" height="17">
              <rect
                x="2.6"
                y="3.6"
                width="14.8"
                height="10"
                rx="1.4"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
              />
              <path
                d="M5.6 10.4l2-2.6 1.8 2 1.6-2.4 1.4 1.6"
                fill="none"
                stroke="currentColor"
                stroke-width="1.3"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
              <path d="M8 16.4h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
            <!-- 带天线的板子：这一页管的是**田里那台盒子**（自己拨号连上来的），
                 与上面那条「显示器」（跑后端这台机器）要在视觉上分得开 -->
            <svg v-else-if="item.icon === 'Devices'" viewBox="0 0 20 20" width="17" height="17">
              <path d="M10 6.2V3.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
              <circle cx="10" cy="2.5" r="1.1" fill="currentColor" />
              <rect
                x="3"
                y="6.2"
                width="14"
                height="11"
                rx="1.8"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
              />
              <rect
                x="7.6"
                y="9.6"
                width="4.8"
                height="4.2"
                rx="1"
                fill="none"
                stroke="currentColor"
                stroke-width="1.3"
              />
              <path
                d="M5.2 9.8v3.6M14.8 9.8v3.6"
                stroke="currentColor"
                stroke-width="1.3"
                stroke-linecap="round"
              />
            </svg>
            <svg v-else viewBox="0 0 20 20" width="17" height="17">
              <circle cx="10" cy="10" r="2.6" fill="none" stroke="currentColor" stroke-width="1.5" />
              <path
                d="M10 2.6v2.2M10 15.2v2.2M2.6 10h2.2M15.2 10h2.2M5 5l1.6 1.6M13.4 13.4 15 15M15 5l-1.6 1.6M6.6 13.4 5 15"
                stroke="currentColor"
                stroke-width="1.4"
                stroke-linecap="round"
              />
            </svg>
          </span>
          <span v-if="!ui.sidebarCollapsed" class="app-sidebar__text">
            <span class="app-sidebar__title">{{ item.title }}</span>
            <span class="app-sidebar__hint">{{ item.hint }}</span>
          </span>

          <!-- 未确认告警数：不给红点的话，告警页很容易被忘掉 -->
          <span
            v-if="item.name === 'alerts' && alerts.unacked > 0"
            class="app-sidebar__badge"
            :aria-label="`${alerts.unacked} 条未确认告警`"
          >
            {{ alerts.unacked > 99 ? '99+' : alerts.unacked }}
          </span>
        </button>
      </li>
    </ul>

    <!-- 原先此处固定显示 `v0.1.0` 与 `后端 127.0.0.1:8000`：
         版本号硬编码会与真实版本漂移，真实版本在顶栏状态提示里（取自 /api/health），
         后端地址在「系统设置」页已有。这里去掉，避免每页都挂着开发者信息。 -->
  </nav>
</template>

<style scoped lang="scss">
.app-sidebar {
  display: flex;
  flex-direction: column;
  width: var(--ip-sidebar-w);
  flex: 0 0 auto;
  background: var(--ip-panel);
  border-right: 1px solid var(--ip-line);
  transition: width var(--ip-dur) var(--ip-ease);
  overflow: hidden;

  &--collapsed {
    width: var(--ip-sidebar-w-collapsed);
  }

  &__brand {
    display: flex;
    align-items: center;
    gap: 9px;
    height: var(--ip-header-h);
    padding: 0 14px;
    border-bottom: 1px solid var(--ip-line);
    flex: 0 0 auto;
  }

  &__brand-text {
    display: flex;
    flex-direction: column;
    line-height: 1.2;
    min-width: 0;

    strong {
      font-size: var(--ip-fs-sm);
      font-weight: 600;
      color: var(--ip-text);
      white-space: nowrap;
    }

    small {
      font-size: var(--ip-fs-xs);
      letter-spacing: 0.06em;
      color: var(--ip-text-3);
      white-space: nowrap;
    }
  }

  &__list {
    flex: 1 1 auto;
    padding: 8px 0;
    overflow-y: auto;
    overflow-x: hidden;
  }

  &__item {
    position: relative;
    display: flex;
    align-items: center;
    gap: 10px;
    width: calc(100% - 12px);
    margin: 2px 6px;
    padding: 8px 10px;
    background: transparent;
    border: none;
    border-radius: var(--ip-radius-sm);
    color: var(--ip-text-2);
    text-align: left;
    cursor: pointer;
    transition:
      background var(--ip-dur-fast) var(--ip-ease),
      color var(--ip-dur-fast) var(--ip-ease),
      transform var(--ip-dur-fast) var(--ip-ease);

    &:hover {
      background: var(--ip-primary-weak);
      color: var(--ip-text);
    }

    /* 按压反馈：点下去轻微缩一下，给出"按到了"的手感 */
    &:active {
      transform: scale(0.98);
    }

    &.is-active {
      background: var(--ip-primary-weak);
      color: var(--ip-text);
    }

    /* 左侧指示条常驻但压扁，激活时展开 —— 高度方向的自然滑动，
       比每次进出都凭空冒出/消失一根线更顺滑 */
    &::before {
      content: '';
      position: absolute;
      left: 0;
      top: 7px;
      bottom: 7px;
      width: 3px;
      border-radius: 0 2px 2px 0;
      background: var(--ip-primary);
      transform: scaleY(0);
      transform-origin: center;
      transition: transform var(--ip-dur) var(--ip-ease);
    }

    &.is-active::before {
      transform: scaleY(1);
    }
  }

  &__icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    flex: 0 0 auto;
    color: inherit;
    transition: color var(--ip-dur-fast) var(--ip-ease);
  }

  /* 悬停/激活时图标比文字先一步染上主色，导航的"指针感"更明确 */
  &__item:hover &__icon,
  &__item.is-active &__icon {
    color: var(--ip-primary-hover);
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
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
}
/** 未确认告警数角标：小圆点 + 数字，收起侧栏时也显示 */
.app-sidebar__badge {
  flex: 0 0 auto;
  margin-left: auto;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--ip-danger);
  color: var(--ip-text-inv);
  font-size: var(--ip-fs-xs);
  font-weight: 600;
  line-height: 18px;
  text-align: center;
  animation: ip-badge-pulse 2.4s var(--ip-ease) infinite;
}

/* 告警角标脉冲：只在数字>0 时挂上；幅度刻意很小，是"呼吸"不是"闪烁" */
@keyframes ip-badge-pulse {
  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(217, 83, 79, 0);
  }
  50% {
    box-shadow: 0 0 0 4px rgba(217, 83, 79, 0.18);
  }
}
</style>


