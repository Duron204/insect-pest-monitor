<script setup lang="ts">
/**
 * 本机（树莓派直连）摄像头面板。
 *
 * 与「摄像头实时检测」的区别，这是现场部署时最容易混的一点：
 *   * 实时检测 = 用**打开网页那台设备**（手机/笔记本）的摄像头，依赖浏览器权限
 *   * 本面板   = 用**边缘盒子自己插着**的摄像头（Pi 排线摄像头 / USB），
 *                盒子按间隔自己抓拍，不需要人守着、也不需要浏览器
 * 因此本面板的抓拍会直接落成历史任务，可进看板与报告。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton, ElTag } from 'element-plus'
import { localCameraApi } from '@/api'
import { notifyApiError, notifySuccess, notifyWarning } from '@/utils/notify'
import type { LocalCameraStatusOut } from '@/types'

const router = useRouter()

const status = ref<LocalCameraStatusOut | null>(null)
const error = ref<string | null>(null)
const busy = ref<'capture' | 'start' | 'stop' | null>(null)
let pollTimer: number | null = null

const running = computed(() => status.value?.running === true)
const backendLabel = computed(() => status.value?.backend ?? '—')
/** 后端是否跑在树莓派上 —— 决定这个面板"控制的是谁"的说法 */
const isPi = computed(() => status.value?.host?.is_raspberry_pi === true)
const hostModel = computed(() => status.value?.host?.model ?? '')
const hostLabel = computed(() => {
  const host = status.value?.host
  if (!host) return '未知设备'
  return `${host.platform} · ${host.machine}`
})
/** 注意 `isPi.value`：脚本里 ref 不会自动解包，漏掉 `.value` 会得到"永远为真"的 ref 对象 */
const title = computed(() => (isPi.value ? '本机摄像头（边缘盒子自采）' : '本机摄像头（开发机）'))
const intervalLabel = computed(() => {
  const minutes = status.value?.interval_min ?? 0
  return minutes > 0 ? `${minutes} 分钟` : '未启用定时（仅手动抓拍）'
})
const lastCaptureText = computed(() => {
  const raw = status.value?.last_capture_at
  if (!raw) return '尚未抓拍'
  // 后端统一以 naive UTC 存储并补 Z，这里按本地时间展示
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString()
})
const nextCaptureText = computed(() => {
  const seconds = status.value?.next_capture_in_s
  if (seconds === null || seconds === undefined || !running.value) return '—'
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return minutes > 0 ? `${minutes} 分 ${rest} 秒后` : `${rest} 秒后`
})

async function refresh(): Promise<void> {
  try {
    status.value = await localCameraApi.status()
    error.value = null
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function onCapture(): Promise<void> {
  busy.value = 'capture'
  try {
    const result = await localCameraApi.captureNow()
    const count = Object.keys(result.counts ?? {}).length
    notifySuccess(
      `已抓拍并入库（任务 #${result.task_id}，检出 ${result.total_objects} 个目标${
        count > 0 ? `，${count} 类` : ''
      }）`
    )
    await refresh()
  } catch (err) {
    // notifyApiError 接受 unknown：catch 里的 err 在 TS 4.4+ 就是 unknown，
    // 直接传 notifyError(string) 会类型报错
    notifyApiError(err)
    await refresh()
  } finally {
    busy.value = null
  }
}

async function onStart(): Promise<void> {
  busy.value = 'start'
  try {
    status.value = await localCameraApi.start()
    if (status.value.running) {
      notifySuccess(`定时抓拍已启动（每 ${status.value.interval_min} 分钟）`)
    } else {
      notifyWarning('未启动：配置里 interval_min 为 0（表示只允许手动抓拍）')
    }
  } catch (err) {
    notifyApiError(err)
  } finally {
    busy.value = null
  }
}

async function onStop(): Promise<void> {
  busy.value = 'stop'
  try {
    status.value = await localCameraApi.stop()
    notifySuccess('已停止定时抓拍并释放摄像头')
  } catch (err) {
    notifyApiError(err)
  } finally {
    busy.value = null
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
  <section class="local-cam ip-panel" aria-label="本机摄像头定时采集">
    <header class="local-cam__head">
      <h2 class="ip-section-title">{{ title }}</h2>
      <span class="local-cam__state">
        <span :class="`ip-dot ip-dot--${running ? 'ok' : 'off'}`" aria-hidden="true" />
        {{ running ? '定时抓拍中' : '未运行' }}
      </span>
    </header>

    <!-- 说明这个面板控制的是**运行后端的这台设备**：现场就是树莓派，
         开发机上则是开发机自己。不写清"本机"是谁，在 Windows 上看到
         "图像源 opencv / 累计抓拍 1" 会让人以为它在管自己的电脑。 -->
    <p class="local-cam__hint">
      <template v-if="isPi">
        控制的是<strong>运行后端的这台树莓派</strong
        ><template v-if="hostModel">（{{ hostModel }}）</template>：用它自己插着的摄像头
        （排线摄像头 / USB）按间隔自动抓拍，不需要人守着、也不依赖浏览器。
      </template>
      <template v-else>
        控制的是<strong>运行后端的这台设备</strong> —— 当前是
        <span class="num">{{ hostLabel }}</span
        >（开发机）。现场部署到树莓派后，同一个面板控制的就是树莓派自己插着的摄像头
        （排线摄像头 / USB）。
      </template>
      抓拍结果会直接进入历史、看板与报告。
    </p>

    <div class="local-cam__controls">
      <ElButton
        type="primary"
        size="small"
        :loading="busy === 'capture'"
        :disabled="busy !== null"
        @click="onCapture"
      >
        立即抓拍
      </ElButton>
      <ElButton
        v-if="!running"
        size="small"
        :loading="busy === 'start'"
        :disabled="busy !== null"
        @click="onStart"
      >
        启动定时抓拍
      </ElButton>
      <ElButton v-else size="small" :loading="busy === 'stop'" :disabled="busy !== null" @click="onStop">
        停止
      </ElButton>
      <ElButton
        v-if="status?.last_task_id"
        size="small"
        text
        type="primary"
        @click="router.push('/history')"
      >
        查看历史记录
      </ElButton>
    </div>

    <p v-if="error" class="local-cam__error">{{ error }}</p>
    <p v-if="status?.last_error" class="local-cam__error">
      上次抓拍失败：{{ status.last_error }}
    </p>

    <dl v-if="status" class="local-cam__grid">
      <div><dt>图像源</dt><dd class="num">{{ backendLabel }}</dd></div>
      <div><dt>抓拍间隔</dt><dd class="num">{{ intervalLabel }}</dd></div>
      <div><dt>累计抓拍</dt><dd class="num">{{ status.capture_count }}</dd></div>
      <div><dt>下次抓拍</dt><dd class="num">{{ nextCaptureText }}</dd></div>
      <div class="local-cam__wide"><dt>上次抓拍</dt><dd class="num">{{ lastCaptureText }}</dd></div>
      <div class="local-cam__wide">
        <dt>最近任务</dt>
        <dd class="num">{{ status.last_task_id ? `#${status.last_task_id}` : '—' }}</dd>
      </div>
    </dl>

    <p v-if="status && !status.enabled" class="local-cam__warn">
      配置里未启用（local_camera.enabled=false）：后端启动时不会自动开始定时抓拍，
      但上面的「立即抓拍」与「启动定时抓拍」仍可手动使用。
    </p>

    <details v-if="status" class="local-cam__sources">
      <summary>可用图像源（现场排障用）</summary>
      <ul>
        <li v-for="item in status.available" :key="item.backend">
          <ElTag size="small" effect="plain" :type="item.available ? 'success' : 'info'">
            {{ item.backend }}
          </ElTag>
          <span class="local-cam__detail">{{ item.detail }}</span>
        </li>
      </ul>
      <p class="local-cam__note">
        配置项为 local_camera.backend（auto 会优先用 picamera2，其次 opencv）；
        没有摄像头时可设 simulated，从 test_media 目录循环读图。
      </p>
    </details>
  </section>
</template>

<style scoped lang="scss">
.local-cam {
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

  &__hint {
    margin: 0;
    font-size: var(--ip-fs-sm);
    line-height: var(--ip-lh);
    color: var(--ip-text-3);
  }

  &__controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
  }

  &__error {
    margin: 0;
    font-size: var(--ip-fs-sm);
    color: var(--ip-danger);
  }

  &__warn {
    margin: 0;
    font-size: var(--ip-fs-sm);
    color: var(--ip-warn);
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.5rem 1rem;
    margin: 0;

    dt {
      font-size: var(--ip-fs-xs);
      color: var(--ip-text-3);
    }

    dd {
      margin: 0.15rem 0 0;
      font-size: var(--ip-fs-body);
    }
  }

  &__wide {
    grid-column: 1 / -1;
  }

  &__sources {
    font-size: var(--ip-fs-sm);

    summary {
      cursor: pointer;
      color: var(--ip-text-3);
    }

    ul {
      margin: 0.5rem 0 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 0.35rem;
    }

    li {
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
    }
  }

  &__detail {
    color: var(--ip-text-3);
  }

  &__note {
    margin: 0.5rem 0 0;
    color: var(--ip-text-3);
  }
}
</style>
