import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { ElLoading } from 'element-plus'

// 样式顺序很重要：Element Plus 基础样式 → 主题覆盖 → 全局基础样式
import 'element-plus/dist/index.css'
import '@/styles/element-dark.scss'
import '@/styles/base.scss'

import App from './App.vue'
import router from './router'
import { setUnauthorizedHandler } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { notifyWarning } from '@/utils/notify'

const app = createApp(App)

app.use(createPinia())
app.use(router)

// 这里刻意**不做 `app.use(ElementPlus)` 全量注册**：
//  - 各组件都在自己的 <script setup> 里显式 import（模板里没有裸用未导入的 el- 组件），
//    显式导入本身就够用，全量注册只会把整个组件库塞进主包；
//  - 全量注册原本唯一不可替代的作用是全局注册 `v-loading` 指令，用下面这行单独注册即可；
//  - 全量注册图标（约 300 个）此前也无一处使用，已移除。
// 实测：这两处去掉后主包从 1,086 KB 降到下方构建日志里的数值。
app.use(ElLoading)

// 主题必须在挂载前落到 <html data-theme>，避免首屏闪白
const ui = useUiStore()
ui.initTheme()

// 401 统一处理：清 token 并跳登录（避免 api 层直接依赖 router）
setUnauthorizedHandler(() => {
  const current = router.currentRoute.value
  if (current.name === 'login') return
  notifyWarning('登录状态已失效，请重新登录')
  void router.replace({
    name: 'login',
    query: current.fullPath === '/' ? undefined : { redirect: current.fullPath }
  })
})

app.mount('#app')
