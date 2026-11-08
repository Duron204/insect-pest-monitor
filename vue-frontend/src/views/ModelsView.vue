<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElProgress } from 'element-plus'
import ModelCard from '@/components/models/ModelCard.vue'
import ModelCompareTable from '@/components/models/ModelCompareTable.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import { useModelsStore } from '@/stores/models'
import { confirmAction, notifyWarning } from '@/utils/notify'
import type { ModelOut } from '@/types'

const models = useModelsStore()

const uploadInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const uploadPercent = ref(0)
const error = ref<string | null>(null)

const recommendation = computed(() => models.recommended)

const showRecommendation = computed(() => {
  const recommended = models.recommended
  if (!recommended) return false
  return !models.isCurrent(recommended)
})

const currentDescription = computed(() => {
  const current = models.currentModel
  if (!current) return models.health?.model_path ?? '未加载'
  const metrics: string[] = [current.arch]
  if (current.map50 !== null) metrics.push(`mAP50 ${current.map50.toFixed(3)}`)
  if (current.map50_95 !== null) metrics.push(`mAP50-95 ${current.map50_95.toFixed(3)}`)
  return `${current.name}（${metrics.join(' · ')}）`
})

function openUpload(): void {
  uploadInput.value?.click()
}

async function onFilePicked(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  const file = files[0]
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.pt')) {
    notifyWarning('仅支持 .pt 权重文件')
    return
  }

  // 加载 .pt 会走 torch.load 反序列化，理论上可执行代码 —— 这**无法在保留 .pt 支持
  // 的前提下消除**（ultralytics 内部调用 torch.load，我们控制不了它的 weights_only）。
  // 因此能做的是：上传前把风险说清楚，让"只传自己训练/可信来源的权重"成为一次明确选择。
  const ok = await confirmAction({
    title: '上传权重前请确认来源',
    message:
      `${file.name}\n\n` +
      '加载 .pt 权重会执行 pickle 反序列化，理论上可执行任意代码。\n' +
      '请只上传你自己训练、或来源可信的权重；不要上传别人发来的、来源不明的 .pt 文件。\n\n' +
      '（对外部署时应改用 ONNX / safetensors 等不含可执行代码的格式。）',
    confirmText: '确认上传',
    danger: true
  })
  if (!ok) return

  uploading.value = true
  uploadPercent.value = 0
  error.value = null
  try {
    const result = await models.uploadModel(file, (ratio) => {
      uploadPercent.value = Math.round(ratio * 100)
    })
    if (!result) error.value = models.error
  } finally {
    uploading.value = false
    uploadPercent.value = 0
  }
}

async function onSelect(model: ModelOut): Promise<void> {
  const ok = await confirmAction({
    title: '切换检测模型',
    message: `确认把当前模型切换到「${model.name}」？\n切换后新的检测请求与实时通道都会使用该权重。`,
    confirmText: '切换'
  })
  if (!ok) return
  error.value = null
  const result = await models.selectModel(model.id)
  if (!result) error.value = models.error
}

onMounted(() => {
  void models.loadRegistry(true)
})
</script>

<template>
  <div class="models-view">
    <input
      ref="uploadInput"
      class="models-view__input"
      type="file"
      accept=".pt"
      aria-label="上传 .pt 权重文件"
      @change="onFilePicked"
    >

    <header class="models-view__head ip-panel">
      <div class="models-view__head-left">
        <h2 class="ip-section-title">模型注册表</h2>
        <p class="models-view__desc">
          当前模型：<span class="models-view__current">{{ currentDescription }}</span>
        </p>
        <p class="models-view__desc ip-dim">
          共 {{ models.items.length }} 个已注册模型 · 推荐模型由后端按精度给出（recommended_id）
        </p>
      </div>
      <div class="models-view__actions">
        <ElButton
          size="small"
          :loading="models.loading"
          aria-label="刷新模型注册表"
          @click="models.loadRegistry(true)"
        >
          刷新
        </ElButton>
        <ElButton
          size="small"
          type="primary"
          :loading="uploading"
          aria-label="上传新的 .pt 模型"
          @click="openUpload"
        >
          上传 .pt
        </ElButton>
      </div>
    </header>

    <div v-if="uploading" class="models-view__upload ip-panel">
      <span class="ip-label">正在上传并注册权重…</span>
      <ElProgress :percentage="uploadPercent" :stroke-width="6" :show-text="false" />
      <span class="num models-view__upload-percent">{{ uploadPercent }}%</span>
    </div>

    <ErrorBanner :message="error" title="模型操作失败" @close="error = null" />

    <section v-if="showRecommendation && recommendation" class="models-view__banner" role="status">
      <span class="models-view__banner-icon" aria-hidden="true">!</span>
      <div class="models-view__banner-body">
        <p class="models-view__banner-title">精度最高，建议切换</p>
        <p class="models-view__banner-text">
          推荐模型「{{ recommendation.name }}」
          <template v-if="recommendation.map50_95 !== null">
            （mAP50-95 {{ recommendation.map50_95.toFixed(3) }}）
          </template>
          精度优于当前模型，点击右侧按钮即可切换。
        </p>
      </div>
      <ElButton
        size="small"
        type="warning"
        plain
        :loading="models.switching"
        :aria-label="`切换到推荐模型 ${recommendation.name}`"
        @click="onSelect(recommendation)"
      >
        切换到推荐模型
      </ElButton>
    </section>

    <EmptyState
      v-if="models.items.length === 0 && !models.loading"
      description="注册表里还没有模型"
      hint="后端会扫描 weights 与 runs 目录；也可以直接上传一个 .pt 权重文件"
      action-text="上传 .pt 模型"
      @action="openUpload"
    />

    <template v-else>
      <section class="models-view__grid">
        <ModelCard
          v-for="model in models.items"
          :key="model.id"
          :model="model"
          :is-current="models.isCurrent(model)"
          :is-recommended="models.isRecommended(model)"
          :switching="models.switching"
          @select="onSelect"
        />
      </section>

      <ModelCompareTable
        :models="models.items"
        :current-path="models.currentPath"
        :recommended-id="models.recommendedId"
        :current-model-id="models.currentModelId"
      />
    </template>
  </div>
</template>

<style scoped lang="scss">
.models-view {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;

  &__input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
    pointer-events: none;
  }

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    padding: 12px;
  }

  &__head-left {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }

  &__desc {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__current {
    font-family: var(--ip-font-mono);
    color: var(--ip-primary-hover);
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__upload {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
  }

  &__upload-percent {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
    flex: 0 0 auto;
  }

  &__banner {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
    background: var(--ip-panel);
    border: 1px solid var(--ip-warn-line);
    border-radius: var(--ip-radius);
  }

  &__banner-icon {
    flex: 0 0 auto;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: var(--ip-warn);
    color: var(--ip-text-inv);
    font-size: var(--ip-fs-sm);
    font-weight: 700;
    text-align: center;
    line-height: 20px;
  }

  &__banner-body {
    flex: 1 1 auto;
    min-width: 0;
  }

  &__banner-title {
    font-size: var(--ip-fs-sm);
    font-weight: 600;
    color: var(--ip-warn);
  }

  &__banner-text {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(288px, 1fr));
    gap: var(--ip-gap);
  }
}
</style>
