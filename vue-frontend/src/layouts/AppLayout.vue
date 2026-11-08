<script setup lang="ts">
/**
 * 应用外壳：左侧固定导航 + 顶栏 + 内容区。
 *
 * 这里原先还有一整套手机端适配（底部导航、紧凑模式、断点监听）。
 * 按"只在电脑上使用"的要求已全部移除：窄屏适配代码不再需要，
 * 少一套并行导航也少一处会与实际界面不同步的地方。
 */
import { RouterView } from 'vue-router'
import AppHeader from '@/components/common/AppHeader.vue'
import AppSidebar from '@/components/common/AppSidebar.vue'
</script>

<template>
  <div class="app-layout">
    <AppSidebar />

    <div class="app-layout__main">
      <AppHeader />
      <main class="app-layout__content">
        <!-- 路由切换的进场/退场：淡入 + 8px 位移，out-in 避免新旧页面同时在
             flex 容器里撑高。动画令牌走 tokens.scss，reduced-motion 时由
             base.scss 的全局规则归零。 -->
        <RouterView v-slot="{ Component }">
          <Transition name="page" mode="out-in">
            <component :is="Component" />
          </Transition>
        </RouterView>
      </main>
    </div>
  </div>
</template>

<style scoped lang="scss">
.app-layout {
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: var(--ip-bg);

  &__main {
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
  }

  &__content {
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
}

/* 路由过渡：进入从下方 8px 淡入，离开向上 6px 淡出 —— 方向感与"前进"一致 */
.page-enter-active,
.page-leave-active {
  transition:
    opacity var(--ip-dur) var(--ip-ease),
    transform var(--ip-dur) var(--ip-ease);
}

.page-enter-from {
  opacity: 0;
  transform: translateY(8px);
}

.page-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}</style>

