<script setup lang="ts">
import { useRouter } from 'vue-router'
import { ElButton } from 'element-plus'

const router = useRouter()

function goHome(): void {
  void router.replace({ name: 'detect' })
}
</script>

<template>
  <div class="not-found">
    <div class="not-found__panel">
      <p class="not-found__code num">404</p>
      <h1 class="not-found__title">页面不存在</h1>
      <p class="not-found__hint">
        该地址没有对应的页面。可用页面见左侧导航：检测台 / 历史记录 / 统计看板 / 告警中心 /
        模型管理 / 虫害知识库 / 系统设置。
      </p>
      <div class="not-found__actions">
        <ElButton type="primary" size="small" aria-label="返回检测台" @click="goHome">
          返回检测台
        </ElButton>
        <ElButton size="small" text aria-label="返回上一页" @click="router.back()">
          返回上一页
        </ElButton>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.not-found {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
  background: var(--ip-bg);

  &__panel {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    max-width: 420px;
    padding: 32px 28px;
    text-align: center;
    background: var(--ip-panel);
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-lg);
    box-shadow: var(--ip-shadow-md);
  }

  &__code {
    font-size: 42px;
    font-weight: 700;
    line-height: 1;
    color: var(--ip-primary);
    letter-spacing: 0.04em;
  }

  &__title {
    font-size: 16px;
    font-weight: 600;
  }

  &__hint {
    font-size: var(--ip-fs-xs);
    line-height: 1.65;
    color: var(--ip-text-2);
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
  }
}
</style>
