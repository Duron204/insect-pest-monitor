import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { getToken } from '@/api/client'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' }
  },
  {
    path: '/',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: '',
        redirect: '/detect'
      },
      {
        path: 'detect',
        name: 'detect',
        component: () => import('@/views/DetectView.vue'),
        meta: { title: '检测台', icon: 'Aim' }
      },
      {
        path: 'assistant',
        name: 'assistant',
        component: () => import('@/views/AssistantView.vue'),
        meta: { title: 'AI 助手', icon: 'Spark' }
      },
      {
        path: 'history',
        name: 'history',
        component: () => import('@/views/HistoryView.vue'),
        meta: { title: '历史记录', icon: 'Files' }
      },
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '统计看板', icon: 'DataLine' }
      },
      {
        path: 'alerts',
        name: 'alerts',
        component: () => import('@/views/AlertsView.vue'),
        meta: { title: '告警中心', icon: 'Bell' }
      },
      {
        path: 'models',
        name: 'models',
        component: () => import('@/views/ModelsView.vue'),
        meta: { title: '模型管理', icon: 'Cpu' }
      },
      {
        path: 'species',
        name: 'species',
        component: () => import('@/views/SpeciesView.vue'),
        meta: { title: '虫害知识库', icon: 'Notebook' }
      },
      {
        // 挂在账号下的树莓派（田里的盒子）。注意 `device` 这个 path 已被
        // 「设备状态」占着，所以这里用复数 —— 两者含义完全不同，见两页的说明。
        path: 'devices',
        name: 'devices',
        component: () => import('@/views/DevicesView.vue'),
        meta: { title: '我的设备', icon: 'Devices' }
      },
      {
        // 从列表点进来的详情页，**不进侧栏**（侧栏高亮靠 route.name，多一层反而更乱）
        path: 'devices/:id',
        name: 'device-detail',
        component: () => import('@/views/DeviceDetailView.vue'),
        meta: { title: '设备详情', icon: 'Devices' }
      },
      {
        path: 'device',
        name: 'device',
        component: () => import('@/views/DeviceStatusView.vue'),
        meta: { title: '设备状态', icon: 'Monitor' }
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/SettingsView.vue'),
        meta: { title: '系统设置', icon: 'Setting' }
      }
    ]
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { public: true, title: '页面不存在' }
  }
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 })
})

const APP_TITLE = '病虫害智能监测系统'

router.beforeEach((to) => {
  const token = getToken()
  const hasToken = token !== null && token.length > 0
  const isPublic = to.meta.public === true

  if (!hasToken && !isPublic) {
    return {
      name: 'login',
      query: to.fullPath === '/' ? undefined : { redirect: to.fullPath }
    }
  }
  if (hasToken && to.name === 'login') {
    return { name: 'detect' }
  }
  return true
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : ''
  document.title = title ? `${title} · ${APP_TITLE}` : APP_TITLE
})

export default router
