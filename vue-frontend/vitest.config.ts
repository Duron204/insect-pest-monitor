import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

/**
 * 前端单元测试配置。
 *
 * 只跑**纯逻辑与自包含组件**：不在这里注册 Element Plus（那是 main.ts 的事），
 * 所以用到 Element Plus 的组件需要单独搭测试壳 —— 目前不测它们，
 * 因为它们的行为主要在交互，靠 Playwright 冒烟更划算。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.spec.ts']
  }
})
