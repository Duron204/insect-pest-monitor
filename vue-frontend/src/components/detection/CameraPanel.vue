<script setup lang="ts">
import { computed } from 'vue'
import { ElButton, ElSlider } from 'element-plus'
import StatCard from '@/components/common/StatCard.vue'

const props = withDefaults(
  defineProps<{
    capturing: boolean
    starting: boolean
    supported: boolean
    error: string | null
    /** WS 通道状态文案（真实数据驱动，不做假动画） */
    socketLabel: string
    socketTone: 'ok' | 'warn' | 'err' | 'off'
    actualFps: number
    sentFrames: number
    droppedHint?: string
    uniqueObjects: number
    totalObjects: number
    activeTracks: number
    elapsedSeconds: number
    targetFps: number
    quality: number
    selectedModelName: string
  }>(),
  {
    droppedHint: ''
  }
)

const emit = defineEmits<{
  (event: 'start'): void
  (event: 'stop'): void
  (event: 'update:targetFps', value: number): void
  (event: 'update:quality', value: number): void
}>()

const toneClass = computed(() => `ip-dot ip-dot--${props.socketTone}`)

/**
 * `supported` 为 false 时按钮会被禁用 —— 但"只禁用不说话"，用户只会觉得
 * "摄像头打不开"。这里给出具体原因与可执行的解决办法。
 *
 * 最容易被忽略的坑：浏览器只在**安全上下文**（https 或 localhost/127.0.0.1）
 * 下才暴露 navigator.mediaDevices。若用局域网 IP 打开页面（田间用手机访问
 * 就是这个场景），mediaDevices 直接是 undefined，摄像头必然起不来。
 */
const unsupportedReason = computed(() => {
  if (props.supported) return ''
  const secure = typeof window !== 'undefined' ? window.isSecureContext : true
  if (!secure) {
    return (
      '摄像头需要「安全上下文」才能使用：浏览器只在 https 或 localhost / 127.0.0.1 下开放摄像头。' +
      '当前页面不是安全上下文（例如用局域网 IP 如 http://192.168.x.x:3000 打开）。' +
      '请改用 http://127.0.0.1:3000 访问，或为该站点配置 HTTPS。'
    )
  }
  return '当前浏览器不支持摄像头采集（navigator.mediaDevices 不可用），请使用 Chrome / Edge 等现代浏览器。'
})

const elapsedText = computed(() => {
  const total = Math.max(0, Math.floor(props.elapsedSeconds))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(minutes)}:${pad(seconds)}`
})

function onFps(value: number | number[]): void {
  if (typeof value === 'number') emit('update:targetFps', value)
}

function onQuality(value: number | number[]): void {
  if (typeof value === 'number') emit('update:quality', value)
}
</script>

<template>
  <section class="camera-panel ip-panel" aria-label="摄像头实时检测">
    <header class="camera-panel__head">
      <h2 class="ip-section-title">摄像头实时检测</h2>
      <span class="camera-panel__state">
        <span :class="toneClass" aria-hidden="true" />
        {{ socketLabel }}
      </span>
    </header>

    <div class="camera-panel__controls">
      <ElButton
        v-if="!capturing"
        type="primary"
        size="small"
        :loading="starting"
        :disabled="!supported"
        aria-label="开始摄像头实时检测"
        @click="emit('start')"
      >
        开启摄像头
      </ElButton>
      <ElButton
        v-else
        size="small"
        aria-label="停止摄像头实时检测"
        @click="emit('stop')"
      >
        停止检测
      </ElButton>
      <span class="camera-panel__fps num">
        {{ actualFps > 0 ? `${actualFps.toFixed(1)} fps` : '—' }}
      </span>
      <span class="camera-panel__model ip-truncate" :title="selectedModelName">
        {{ selectedModelName }}
      </span>
    </div>

    <p v-if="error" class="camera-panel__error">{{ error }}</p>
    <p v-else-if="unsupportedReason" class="camera-panel__warn">{{ unsupportedReason }}</p>
    <p v-else-if="droppedHint" class="camera-panel__warn">{{ droppedHint }}</p>

    <div class="camera-panel__grid">
      <StatCard label="去重虫数" :value="uniqueObjects" emphasis hint="按轨迹去重" />
      <StatCard label="累计框数" :value="totalObjects" hint="所有已处理帧" />
      <StatCard label="活动轨迹" :value="activeTracks" />
      <StatCard label="运行时长" :value="elapsedText" hint="服务端计时" />
    </div>

    <div class="camera-panel__sliders">
      <div class="camera-panel__field">
        <div class="camera-panel__label-row">
          <label class="ip-label" for="camera-fps">采集帧率</label>
          <span class="num camera-panel__value">{{ targetFps }} fps</span>
        </div>
        <ElSlider
          id="camera-fps"
          :model-value="targetFps"
          :min="1"
          :max="30"
          :step="1"
          :show-tooltip="false"
          aria-label="采集帧率"
          @update:model-value="onFps"
        />
      </div>
      <div class="camera-panel__field">
        <div class="camera-panel__label-row">
          <label class="ip-label" for="camera-quality">JPEG 质量</label>
          <span class="num camera-panel__value">{{ quality.toFixed(2) }}</span>
        </div>
        <ElSlider
          id="camera-quality"
          :model-value="quality"
          :min="0.3"
          :max="0.95"
          :step="0.05"
          :show-tooltip="false"
          aria-label="JPEG 质量"
          @update:model-value="onQuality"
        />
      </div>
    </div>

    <p class="camera-panel__note">
      已发送 <span class="num">{{ sentFrames }}</span> 帧 · 单实例设计：同时只允许 1 路实时流，
      若提示通道占用请先关闭其它页面
    </p>
  </section>
</template>

<style scoped lang="scss">
.camera-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__state {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__controls {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  &__fps {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__model {
    margin-left: auto;
    max-width: 130px;
    font-family: var(--ip-font-mono);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__error {
    font-size: var(--ip-fs-xs);
    color: var(--ip-danger);
    line-height: 1.5;
  }

  &__warn {
    font-size: var(--ip-fs-xs);
    color: var(--ip-warn);
    line-height: 1.5;
  }

  &__grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }

  &__sliders {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 6px;
    border-top: 1px solid var(--ip-line-soft);
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__label-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__value {
    font-size: var(--ip-fs-xs);
    color: var(--ip-primary-hover);
  }

  &__note {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    line-height: 1.5;
  }
}
</style>
