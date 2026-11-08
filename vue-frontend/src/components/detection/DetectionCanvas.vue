<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElButton, ElTooltip } from 'element-plus'
import EmptyState from '@/components/common/EmptyState.vue'
import { useAnnotatedCanvas } from '@/composables/useAnnotatedCanvas'
import type { DetectionRow } from '@/types'

const props = withDefaults(
  defineProps<{
    /** 底图地址；摄像头叠加场景传空字符串 */
    imageUrl: string
    rows: DetectionRow[]
    hoveredKey: string | null
    selectedKey: string | null
    /** 摄像头叠加模式：只画框，底图由外部 <video> 提供 */
    overlayOnly?: boolean
    /** 叠加模式下的基准尺寸（抽帧 JPEG 尺寸） */
    baseWidth?: number
    baseHeight?: number
    /** 画面标识，展示在左上角 */
    frameLabel?: string
    emptyTitle?: string
    emptyHint?: string
    emptyActionText?: string
    loading?: boolean
  }>(),
  {
    overlayOnly: false,
    baseWidth: 0,
    baseHeight: 0,
    frameLabel: '',
    emptyTitle: '尚未选择检测素材',
    emptyHint: '从左侧选择图片、文件夹或视频，或开启摄像头实时检测',
    emptyActionText: '',
    loading: false
  }
)

const emit = defineEmits<{
  (event: 'select', key: string | null): void
  (event: 'hover', key: string | null): void
  (event: 'empty-action'): void
}>()

const stageRef = ref<HTMLElement | null>(null)
const rowsRef = computed(() => props.rows)
const hoveredRef = computed(() => props.hoveredKey)
const selectedRef = computed(() => props.selectedKey)
const imageUrlRef = computed(() => props.imageUrl)

const canvas = useAnnotatedCanvas({
  containerRef: stageRef,
  imageUrl: imageUrlRef,
  rows: rowsRef,
  hoveredKey: hoveredRef,
  selectedKey: selectedRef,
  manualRender: true,
  onSelect: (key) => emit('select', key),
  onHover: (key) => emit('hover', key)
})

const hasContent = computed(() => {
  if (props.overlayOnly) {
    // 摄像头场景：底图由外部 <video> 提供；
    // 只有在「已经有基准尺寸或已有检测结果」时才认为画面就绪，否则展示空状态引导。
    return props.baseWidth > 0 || props.rows.length > 0
  }
  return props.imageUrl.length > 0
})

const showEmpty = computed(() => !hasContent.value && !props.loading)

function redraw(): void {
  if (props.overlayOnly) canvas.drawOverlayOnly()
  else canvas.render()
}

onMounted(() => {
  canvas.observeContainer()
  if (props.overlayOnly) canvas.setManualSize(props.baseWidth, props.baseHeight)
  else void canvas.loadImage(props.imageUrl)
  redraw()
})

watch(
  () => [props.imageUrl, props.overlayOnly] as const,
  () => {
    if (props.overlayOnly) canvas.setManualSize(props.baseWidth, props.baseHeight)
    else void canvas.loadImage(props.imageUrl)
    redraw()
  }
)

watch(
  () => [props.baseWidth, props.baseHeight] as const,
  ([width, height]) => {
    if (!props.overlayOnly) return
    canvas.setManualSize(width, height)
  }
)

watch(
  () => props.rows,
  () => redraw()
)

watch([() => props.hoveredKey, () => props.selectedKey], () => redraw())

watch(
  () => canvas.zoom.value,
  () => redraw()
)

defineExpose({ redraw })
</script>

<template>
  <div class="detection-canvas">
    <div class="detection-canvas__toolbar">
      <div class="detection-canvas__label-group">
        <span class="detection-canvas__chip" :title="frameLabel">
          {{ frameLabel || '画面' }}
        </span>
        <span class="detection-canvas__chip detection-canvas__chip--muted num">
          {{ canvas.imageWidth.value }}×{{ canvas.imageHeight.value }}
        </span>
        <span class="detection-canvas__chip detection-canvas__chip--muted num">
          {{ canvas.zoomPercent.value }}%
        </span>
      </div>
      <div class="detection-canvas__actions">
        <ElTooltip content="缩小（滚轮向下）" placement="bottom">
          <ElButton
            text
            size="small"
            :disabled="!canvas.canZoomOut.value"
            aria-label="缩小画面"
            @click="canvas.zoomOut()"
          >
            －
          </ElButton>
        </ElTooltip>
        <ElTooltip content="放大（滚轮向上）" placement="bottom">
          <ElButton
            text
            size="small"
            :disabled="!canvas.canZoomIn.value"
            aria-label="放大画面"
            @click="canvas.zoomIn()"
          >
            ＋
          </ElButton>
        </ElTooltip>
        <ElTooltip content="适应窗口（双击画面同效）" placement="bottom">
          <ElButton text size="small" aria-label="适应窗口" @click="canvas.fit()">适应</ElButton>
        </ElTooltip>
        <ElTooltip content="按原始像素显示" placement="bottom">
          <ElButton text size="small" aria-label="按原始像素显示" @click="canvas.actualSize()">
            1:1
          </ElButton>
        </ElTooltip>
      </div>
    </div>

    <div ref="stageRef" class="detection-canvas__stage">
      <div class="detection-canvas__base">
        <slot name="base" />
      </div>
      <canvas
        :ref="(element) => (canvas.canvasRef.value = element as HTMLCanvasElement | null)"
        class="detection-canvas__layer"
        aria-label="检测结果叠加画布"
        @wheel.prevent="canvas.onWheel"
        @pointerdown="canvas.onPointerDown"
        @pointermove="canvas.onPointerMove"
        @pointerup="canvas.onPointerUp"
        @pointerleave="canvas.onPointerLeave"
        @dblclick="canvas.onDoubleClick"
      />

      <div v-if="showEmpty" class="detection-canvas__empty">
        <EmptyState
          :description="emptyTitle"
          :hint="emptyHint"
          :action-text="emptyActionText"
          @action="emit('empty-action')"
        />
      </div>

      <div v-else-if="canvas.loading.value" class="detection-canvas__hint">
        <span class="detection-canvas__spinner" aria-hidden="true" />
        正在加载图像…
      </div>

      <p v-else-if="canvas.imageError.value" class="detection-canvas__error">
        {{ canvas.imageError.value }}
      </p>
    </div>

    <p class="detection-canvas__tips">
      滚轮缩放 · 拖拽平移 · 悬停高亮 · 单击框选（与右栏和表格联动）
    </p>
  </div>
</template>

<style scoped lang="scss">
.detection-canvas {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1 1 auto;
  background: var(--ip-panel);
  border: 1px solid var(--ip-line);
  border-radius: var(--ip-radius);
  overflow: hidden;

  &__toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 8px;
    border-bottom: 1px solid var(--ip-line);
    background: var(--ip-panel-2);
    flex: 0 0 auto;
  }

  &__label-group {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  &__chip {
    font-size: var(--ip-fs-xs);
    padding: 2px 7px;
    border-radius: var(--ip-radius-sm);
    border: 1px solid var(--ip-line);
    color: var(--ip-text-2);
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__chip--muted {
    color: var(--ip-text-3);
    font-family: var(--ip-font-mono);
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 2px;
    flex: 0 0 auto;
  }

  &__stage {
    position: relative;
    flex: 1 1 auto;
    min-height: 240px;
    background: var(--ip-bg-deep);
    overflow: hidden;
    touch-action: none;
  }

  &__base {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
  }

  &__layer {
    position: absolute;
    inset: 0;
    display: block;
    cursor: crosshair;
  }

  &__empty {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--ip-bg-deep);
  }

  &__hint {
    position: absolute;
    left: 12px;
    bottom: 12px;
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 5px 10px;
    border-radius: 999px;
    background: var(--ip-panel);
    border: 1px solid var(--ip-line);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__spinner {
    width: 13px;
    height: 13px;
    border: 2px solid var(--ip-line-strong);
    border-top-color: var(--ip-primary);
    border-radius: 50%;
    animation: canvas-spin 720ms linear infinite;
  }

  &__error {
    position: absolute;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    padding: 8px 14px;
    border-radius: var(--ip-radius-sm);
    border: 1px solid var(--ip-danger);
    background: var(--ip-panel);
    font-size: var(--ip-fs-xs);
    color: var(--ip-danger);
  }

  &__tips {
    flex: 0 0 auto;
    padding: 5px 10px;
    border-top: 1px solid var(--ip-line);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    background: var(--ip-panel-2);
  }
}

@keyframes canvas-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
