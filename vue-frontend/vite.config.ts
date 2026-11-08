import { fileURLToPath, URL } from 'node:url'

import { defineConfig, type PluginOption } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * Element Plus 按需自动导入。
 *
 * 说明：这里用**可选加载**的方式引入 `unplugin-auto-import` / `unplugin-vue-components`。
 * - 两个依赖已写入 package.json；装好后会自动启用 ElementPlusResolver 按需导入。
 * - 若某个依赖不可用（例如离线环境下 npm install 失败），这里会安静地跳过，
 *   由 `src/main.ts` 里的 `app.use(ElementPlus)` + 全量图标注册兜底，功能不受影响。
 *
 * 图表已确认**不依赖 echarts**：`src/components/charts/*.vue` 全部是手写内联 SVG，
 * 因此 echarts / vue-echarts 缺失也不会影响看板渲染。
 */
async function elementPlusAutoImport(): Promise<PluginOption[]> {
  const plugins: PluginOption[] = []
  try {
    const { default: AutoImport } = await import('unplugin-auto-import/vite')
    const { default: Components } = await import('unplugin-vue-components/vite')
    const { ElementPlusResolver } = await import('unplugin-vue-components/resolvers')
    plugins.push(
      AutoImport({
        resolvers: [ElementPlusResolver({ importStyle: false })],
        dts: false
      }),
      Components({
        // 全量样式已在 main.ts 引入 element-plus/dist/index.css，
        // 这里关闭按需样式，避免每个组件重复注入样式文件。
        resolvers: [ElementPlusResolver({ importStyle: false })],
        dts: false
      })
    )
  } catch {
    // 依赖未安装：退回 main.ts 的全量注册
  }
  return plugins
}

export default defineConfig(async () => ({
  plugins: [vue(), ...(await elementPlusAutoImport())],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    // 显式绑定 IPv4：Vite 默认监听的 localhost 在 Windows 上可能只落在 ::1，
    // 导致按文档用 http://127.0.0.1:3000 访问时连不上（实测 curl 返回 000）。
    // 田间用手机访问时把这里改成 '0.0.0.0'（同时后端 start_backend.bat 的 --host 也要改）。
    host: '127.0.0.1',
    port: 3000,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      },
      '/media': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      },
      '/ws': {
        target: 'ws://127.0.0.1:8000',
        changeOrigin: true,
        ws: true
      }
    }
  },
  build: {
    chunkSizeWarningLimit: 1500
  }
}))
