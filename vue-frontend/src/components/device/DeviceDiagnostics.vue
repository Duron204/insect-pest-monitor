<script setup lang="ts">
/**
 * 设备详情里的「调试信息」面板。
 *
 * 这里每一个数字都是**盒子自己报的**（`/ws/agent` 的 status 报文），中心只转存。
 * 之所以要有这一块：中心能算的只有 `online`（最后一次听到它是什么时候），
 * 答不了现场真正卡人的那句「在线，但为什么没画面 / 为什么结果一直不来」。
 *
 * 两条显示纪律，**比面板上任何一行都重要**：
 *
 *   1. **`null` 一律显示「未上报」，绝不显示 0 或 false。**
 *      「推了 0 帧」和「不知道推了几帧」是两件事：前者指向摄像头坏了，
 *      后者只是盒子跑的旧版本、不发 status 报文。把后者显示成前者，
 *      会让看的人去拆一台好盒子。
 *   2. **最近错误必须带着时刻显示，且说明它不会被自动清空。**
 *      否则一条三天前的瞬时错误会被当成正在发生的故障。
 *
 * 面板默认收起；**一旦盒子报了错误就自动展开** —— 报错正是这块面板存在的理由，
 * 让人先点开再看到，等于没提醒。
 */
import { computed, ref, watch } from 'vue'
import { formatDateTime } from '@/utils'
import type { DeviceOut } from '@/types'

const props = defineProps<{ device: DeviceOut }>()

const NOT_REPORTED = '未上报'

/** 收起/展开。`:open` 与 `@toggle` 成对出现 —— 用户手动点了要能同步回来 */
const open = ref(false)

/** 三态：null（没报）与 true / false 必须显示成三种不同的东西 */
function triState(value: boolean | null, whenTrue: string, whenFalse: string): string {
  if (value === null) return NOT_REPORTED
  return value ? whenTrue : whenFalse
}

function orUnreported(value: number | null): string {
  return value === null ? NOT_REPORTED : String(value)
}

/**
 * 画面来源 → 人话。这是本面板里**最容易让人误解**的一项：
 * `simulated` 表示看到的画面其实是 `test_media/` 里的训练照片，并不是真实摄像头，
 * 所以必须单独指名道姓地说出来，不能只显示一个中性词。
 */
function cameraBackendText(backend: string | null): string {
  switch (backend) {
    case null:
    case undefined:
      return NOT_REPORTED
    case 'picamera2':
      return 'CSI 摄像头（排线）'
    case 'opencv':
      return 'USB 摄像头'
    case 'simulated':
      return '模拟图（训练照片）'
    default:
      return backend
  }
}

const cameraSimulated = computed(
  () => props.device.agent_camera_backend === 'simulated'
)

type Tone = 'ok' | 'warn' | 'dim'

interface Row {
  label: string
  value: string
  tone: Tone
  hint?: string
}

const everReported = computed(() => props.device.agent_status_at !== null)

const pairingText = computed(() => {
  switch (props.device.status) {
    case 'active':
      return '已绑定'
    case 'pending':
      return '待配对'
    case 'unbound':
      return '已解绑'
    default:
      return props.device.status || NOT_REPORTED
  }
})

const rows = computed<Row[]>(() => {
  const device = props.device
  const out: Row[] = [
    {
      label: '配对状态',
      value: pairingText.value,
      tone: device.status === 'active' ? 'ok' : 'warn'
    },
    {
      label: '推流连接',
      value: triState(device.agent_streaming, '已连接', '未连接'),
      tone: device.agent_streaming === null ? 'dim' : device.agent_streaming ? 'ok' : 'warn'
    },
    {
      label: '推流开关（盒子侧）',
      value: triState(device.agent_stream_enabled, '开', '关'),
      tone: device.agent_stream_enabled === null ? 'dim' : device.agent_stream_enabled ? 'ok' : 'warn',
      // 「在线却没画面」最常见的原因就是这一个开关，直接把结论说出来
      hint: device.agent_stream_enabled === false ? '关掉了就必然没有画面' : undefined
    },
    {
      label: '画面来源',
      value: cameraBackendText(device.agent_camera_backend),
      tone:
        device.agent_camera_backend === null
          ? 'dim'
          : device.agent_camera_backend === 'simulated'
            ? 'warn'
            : 'ok',
      // 这一项存在的唯一理由：把"看着像实时、实则播训练图"这件事点破
      hint: cameraSimulated.value
        ? '当前画面是 test_media 里的训练照片，不是真实摄像头——请检查树莓派摄像头是否连接并被识别'
        : undefined
    },
    {
      label: '实时识别（盒子侧）',
      value: triState(device.agent_live_detection, '开', '关'),
      tone: device.agent_live_detection === null ? 'dim' : 'ok'
    },
    {
      label: '已推帧数',
      value: orUnreported(device.agent_frames_sent),
      tone: device.agent_frames_sent === null ? 'dim' : 'ok'
    },
    {
      label: '已上报结果',
      value: orUnreported(device.agent_uploads),
      tone: device.agent_uploads === null ? 'dim' : 'ok'
    },
    {
      label: '待上报积压',
      value: orUnreported(device.agent_pending_uploads),
      tone:
        device.agent_pending_uploads === null
          ? 'dim'
          : device.agent_pending_uploads > 0
            ? 'warn'
            : 'ok',
      hint:
        device.agent_pending_uploads !== null && device.agent_pending_uploads > 0
          ? '有积压说明这台盒子断过网，恢复后会自动补发'
          : undefined
    },
    {
      label: '盒子版本',
      value: device.agent_version?.trim() || NOT_REPORTED,
      tone: device.agent_version ? 'ok' : 'dim'
    },
    {
      label: '最近心跳',
      value: device.last_seen_at ? formatDateTime(device.last_seen_at) : '从未',
      tone: device.online ? 'ok' : 'warn'
    },
    {
      label: '状态上报于',
      value: device.agent_status_at ? formatDateTime(device.agent_status_at) : '从未上报',
      tone: everReported.value ? 'ok' : 'dim'
    }
  ]
  return out
})

/** 有错误、或画面来源是模拟图就自动展开：这正是这块面板存在的理由 */
watch(
  () => [props.device.agent_last_error, props.device.agent_camera_backend],
  ([error, backend]) => {
    if ((error && error.trim()) || backend === 'simulated') open.value = true
  },
  { immediate: true }
)

function onToggle(event: Event): void {
  open.value = (event.target as HTMLDetailsElement).open
}
</script>

<template>
  <details class="diag ip-panel" :open="open" @toggle="onToggle">
    <summary class="diag__summary">
      <span class="diag__title">调试信息</span>
      <span class="diag__hint">
        <template v-if="everReported">盒子自报，约每 10 秒一次</template>
        <template v-else>这台盒子还没有自报过状态</template>
      </span>
      <span class="diag__chevron" aria-hidden="true">{{ open ? '收起' : '展开' }}</span>
    </summary>

    <div class="diag__body">
      <p v-if="!everReported" class="diag__stale">
        没有收到过 status 报文。最常见的原因是这台盒子跑的还是旧版本 ——
        把新代码拷过去重启一次，它就会开始自报。
      </p>

      <dl class="diag__grid">
        <template v-for="row in rows" :key="row.label">
          <dt class="diag__label">{{ row.label }}</dt>
          <dd class="diag__value" :class="`diag__value--${row.tone}`">
            {{ row.value }}
            <span v-if="row.hint" class="diag__note">{{ row.hint }}</span>
          </dd>
        </template>
      </dl>

      <div v-if="device.agent_last_error" class="diag__error">
        <p class="diag__error-text">{{ device.agent_last_error }}</p>
        <p class="diag__error-meta">
          最近一次错误（盒子自报）
          <template v-if="device.agent_last_error_at">
            · <span class="num">{{ formatDateTime(device.agent_last_error_at) }}</span>
          </template>
          · 它不会被自动清空，看时刻就知道是不是陈年旧账
        </p>
      </div>

      <div v-if="cameraSimulated" class="diag__simulated">
        <p class="diag__simulated-text">
          ⚠️ 当前画面来自<b>模拟源</b>（即 <span class="num">test_media/</span> 里的训练照片），<b>不是真实摄像头</b>。
        </p>
        <p class="diag__simulated-meta">
          盒子在 auto 模式下没检测到可用的真实摄像头，于是静默回退到模拟图。
          请到树莓派上确认：摄像头排线是否插好、raspi-config 里摄像头接口是否开启、
          <span class="num">/dev/video*</span> 节点是否存在。修好后重启盒子服务即会切回真实画面。
        </p>
      </div>
      <p v-else-if="everReported" class="diag__clean">盒子最近没有报错。</p>

      <p class="diag__foot">
        数据来源：盒子经 <span class="num">/ws/agent</span> 的 status 报文主动上报，
        中心只做转存。改推流/实时识别开关要在<b>盒子本机</b>的设置页上改 ——
        中心改不了盒子上的配置。
      </p>
    </div>
  </details>
</template>

<style scoped lang="scss">
.diag {
  border: 1px solid var(--ip-line);
  border-radius: var(--ip-radius);
  padding: 0;

  &__summary {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 10px 14px;
    cursor: pointer;
    list-style: none;
    user-select: none;

    &::-webkit-details-marker {
      display: none;
    }
  }

  &__title {
    font-size: var(--ip-fs-sm);
    font-weight: 600;
    color: var(--ip-text);
  }

  &__hint {
    flex: 1 1 auto;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__chevron {
    font-size: var(--ip-fs-xs);
    color: var(--ip-primary);
  }

  &__body {
    border-top: 1px solid var(--ip-line-soft);
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  &__stale {
    margin: 0;
    font-size: var(--ip-fs-xs);
    line-height: 1.7;
    color: var(--ip-warn);
    background: var(--ip-warn-weak);
    border: 1px solid var(--ip-warn-line);
    border-radius: var(--ip-radius-sm);
    padding: 8px 10px;
  }

  &__grid {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 6px 14px;
    margin: 0;
  }

  &__label {
    margin: 0;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    white-space: nowrap;
  }

  &__value {
    margin: 0;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);

    &--ok {
      color: var(--ip-ok);
    }

    &--warn {
      color: var(--ip-warn);
    }

    &--dim {
      // 「未上报」用最弱的颜色：它不是故障，只是没有信息
      color: var(--ip-text-3);
      font-style: italic;
    }
  }

  &__note {
    margin-left: 6px;
    color: var(--ip-text-3);
    font-style: normal;
  }

  &__error {
    border: 1px solid var(--ip-danger-weak);
    background: var(--ip-danger-weak);
    border-radius: var(--ip-radius-sm);
    padding: 8px 10px;
  }

  &__simulated {
    border: 1px solid var(--ip-warn-line);
    background: var(--ip-warn-weak);
    border-radius: var(--ip-radius-sm);
    padding: 8px 10px;
  }

  &__simulated-text {
    margin: 0 0 4px;
    font-size: var(--ip-fs-xs);
    line-height: 1.6;
    color: var(--ip-warn);
    word-break: break-word;
  }

  &__simulated-meta {
    margin: 0;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    line-height: 1.7;
  }

  &__error-text {
    margin: 0 0 4px;
    font-size: var(--ip-fs-xs);
    line-height: 1.6;
    color: var(--ip-danger);
    word-break: break-word;
  }

  &__error-meta {
    margin: 0;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__clean {
    margin: 0;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__foot {
    margin: 0;
    font-size: var(--ip-fs-xs);
    line-height: 1.7;
    color: var(--ip-text-3);
  }
}
</style>
