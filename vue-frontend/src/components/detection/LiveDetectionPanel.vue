<script setup lang="ts">
/**
 * 推流画面的「实时识别」开关。
 *
 * 只对**运行后端的这台机器**生效（现场＝树莓派）。中心不能反向连盒子，
 * 所以这张卡片只在 `agent.enabled=true` 的机器上出现 —— 中心自己的设置页
 * 里不会有它，这是有意为之，不是渲染错误。
 *
 * 开关状态落盘在 `backend/data/agent.json`，重启后保留。开启后每一帧
 * 推流都在盒子上推理并画框，推流自动限制为 2 帧/秒（见后端
 * `_LIVE_DETECTION_MAX_FPS`）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElSwitch, ElTag } from 'element-plus'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import { systemApi } from '@/api'
import type { AgentStatusOut } from '@/types'

const status = ref<AgentStatusOut | null>(null)
const live = ref(false)
const busy = ref(false)
const error = ref<string | null>(null)
let pollTimer: number | null = null

/** 只在配置成边缘代理的机器上出现 */
const visible = computed(() => status.value?.enabled === true)

function applyStatus(next: AgentStatusOut): void {
  status.value = next
  live.value = next.live_detection
}

/** 只取消息、不弹全局 toast —— 这张卡片自己用 ErrorBanner 就地显示 */
function errorText(err: unknown): string {
  const candidate = err as { message?: unknown } | null
  if (candidate && typeof candidate === 'object' && 'message' in candidate) {
    return String(candidate.message ?? '请求失败')
  }
  return err instanceof Error ? err.message : String(err)
}

async function refresh(): Promise<void> {
  // 正在提交时不要用轮询结果把开关打回去：用户刚点的那个值要以接口为准。
  if (busy.value) return
  try {
    applyStatus(await systemApi.agentStatus())
  } catch (err) {
    error.value = errorText(err)
  }
  // 成功的那次轮询**不清除**错误：开关失败的原因只显示 5 秒等于没显示。
  // 错误横幅可以手动关，下一次成功的切换也会清掉它。
}

/**
 * `ElSwitch` 的 before-change：先请求后端，成功才让开关翻转。
 * 失败时返回 false，开关保持原状 —— 不做"先乐观翻转、失败再抖回去"，
 * 那会让人以为已经开了。
 */
async function beforeToggle(): Promise<boolean> {
  const next = !live.value
  busy.value = true
  try {
    applyStatus(await systemApi.setLiveDetection(next))
    error.value = null
    return true
  } catch (err) {
    error.value = errorText(err)
    return false
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await refresh()
  pollTimer = window.setInterval(refresh, 5000)
})

onBeforeUnmount(() => {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<template>
  <section v-if="visible" class="live-detect ip-panel" aria-label="推流画面实时识别">
    <header class="live-detect__head">
      <h2 class="ip-section-title">实时识别（推流画面）</h2>
      <span class="live-detect__state">
        <span :class="`ip-dot ip-dot--${live ? 'ok' : 'off'}`" aria-hidden="true" />
        {{ live ? '已开启' : '已关闭' }}
      </span>
    </header>

    <p class="live-detect__hint">
      控制的是<strong>运行后端的这台盒子</strong>推到中心的实时画面。
      开启后，每一帧都会先在本机推理并画上检测框再发送；推流会自动限制为
      <span class="num">2</span> 帧/秒。
    </p>

    <div class="live-detect__controls">
      <ElSwitch
        v-model="live"
        :loading="busy"
        :disabled="busy || !status?.stream_enabled"
        :before-change="beforeToggle"
        aria-label="在实时画面上识别"
      />
      <span class="live-detect__label">
        {{ live ? '画面中会显示检测框' : '只推送原始画面' }}
      </span>
      <ElTag
        v-if="status"
        size="small"
        effect="plain"
        :type="status.streaming ? 'success' : 'info'"
      >
        {{ status.streaming ? '推流中' : '未推流' }}
      </ElTag>
    </div>

    <ErrorBanner
      v-if="error"
      :message="error"
      title="实时识别设置失败"
      @close="error = null"
    />

    <p v-if="status && !status.stream_enabled" class="live-detect__warn">
      配置文件里关闭了推流（<span class="num">agent.stream_enabled=false</span>），
      实时识别没有可处理的画面。修改 <span class="num">config.yaml</span>
      并重启后端后再打开这个开关。
    </p>
    <p v-else-if="status && !status.running" class="live-detect__warn">
      边缘代理当前未运行（还没配对或启动失败）。开关会先保存，代理启动后生效。
    </p>

    <p class="live-detect__note">
      实时推理会持续占用树莓派 CPU，机身温度和功耗会上升；关闭后推流恢复为原始画面。
      定时抓拍的识别与上报不受这个开关影响。
    </p>
  </section>
</template>

<style scoped lang="scss">
.live-detect {
  display: grid;
  gap: 0.75rem;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  &__state {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: var(--ip-fs-sm);
    color: var(--ip-text-3);
  }

  &__hint,
  &__note {
    margin: 0;
    font-size: var(--ip-fs-sm);
    line-height: var(--ip-lh);
    color: var(--ip-text-3);
  }

  &__controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.75rem;
  }

  &__label {
    font-size: var(--ip-fs-body);
  }

  &__warn {
    margin: 0;
    font-size: var(--ip-fs-sm);
    color: var(--ip-warn);
  }
}
</style>
