<script setup lang="ts">
/**
 * 盒子的实时画面（MJPEG）。
 *
 * 接线只有三步：领一张短期票据 → 拼成地址丢给 `<img>` → 定期换票。
 * 麻烦全在细节上，下面每条都是必须的：
 *
 *   1. **`<img>` 设不了请求头**，所以票据走 query。`streamUrl()` 负责拼。
 *   2. **出错后要重新领票并换一个地址**（带时间戳）。`<img>` 在流出错后
 *      会自动重试**同一个 URL**；只让它自己重试的话，票据一过期画面就永久坏掉，
 *      而且浏览器可能把失败响应缓存住 —— 换 URL 同时解决这两件事。
 *   3. **重试要限速**。设备掉线时 `@error` 会连着来，不限速就是对着票据接口打转。
 *   4. **每 90 秒主动换票**（票据 TTL 120 秒）。这是第 2 条的兜底：连接一直没断、
 *      也没报错，但票据已经老了 —— 不换的话下一次断线就再也连不上。
 *   5. 画面**不画框**（用户选的口径）：推上来的是原始 JPEG，盒子把 CPU 留给定时抓拍。
 *
 * 播放是**按需的**（用户选的口径）：进页面不自动连，点了「查看摄像头」才开始，
 * 点「断开画面」就停。这样不开着详情页也白白占着一条转播连接的事不会发生。
 *
 * 两个容易写错、这里刻意分开的东西：
 *   * **换票定时器是"页面级"的**，只在 onMounted/onBeforeUnmount 里起停。
 *     它不能跟着"设备离线"一起被清掉 —— 清掉之后设备再上线就没人换票了。
 *     （回调里自己判断 `started`，没开看就不去领票。）
 *   * **出错时不清 `src`**：清了 `<img>` 会被 `v-if` 摘掉，于是画面变成一块黑，
 *     而浏览器的实际行为（保留上一帧）其实更好用。改为铺一层"正在重连"的蒙版 ——
 *     蒙版是关键：**绝不能让人把一帧冻住的旧画面当成直播**。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElButton } from 'element-plus'
import { normalizeError } from '@/api'
import { createStreamTicket, streamUrl } from '@/api/devices'
import type { DeviceOut } from '@/types'

const props = defineProps<{ device: DeviceOut }>()

/** 票据 120 秒有效，90 秒换一次：留 30 秒余量，也给一次网络重试的余地 */
const TICKET_REFRESH_MS = 90_000
/** 出错后的冷却。设备离线时 `@error` 反复触发，不限速就是对着票据接口打转 */
const RETRY_DELAY_MS = 4_000
/** 超过这么久还没出现第一帧，就不再只说"正在连接"，把可能的原因一并说清 */
const GRACE_MS = 8_000

const src = ref('')
/** 用户点了「查看摄像头」没有。没点就绝不领票、绝不挂 `<img>` */
const started = ref(false)
/** 这一条连接有没有画出过画面。**只在断开时收回**，换票时不重置（避免每 90 秒闪一下） */
const firstFrame = ref(false)
const waitedTooLong = ref(false)
/** 非空表示画面这条链路出问题了（领票失败或流出错），蒙版据此换成"中断"文案 */
const streamError = ref<string | null>(null)

let refreshTimer: number | null = null
let retryTimer: number | null = null
let graceTimer: number | null = null
let inFlight = false
/** 换地址用的计数器。**每次都必须不一样**，否则浏览器会用缓存里那份失败响应 */
let stamp = 0

const online = computed(() => props.device.online)
const streaming = computed(() => props.device.stream_fps > 0)
const waiting = computed(() => online.value && streamError.value === null && !firstFrame.value)

const coverText = computed(() => {
  if (streamError.value) return streamError.value
  if (!online.value) return null
  if (waitedTooLong.value) {
    return (
      '盒子在线，但还没有画面帧。可能的原因：盒子上推流被关掉了（agent.stream_enabled），' +
      '或者摄像头没有在出图。'
    )
  }
  return '正在握手，通常一两秒内就会出现画面。'
})

async function connect(): Promise<void> {
  if (inFlight || !started.value || !online.value) return
  inFlight = true
  try {
    const { ticket } = await createStreamTicket(props.device.id)
    // 领票期间设备可能刚好掉线了（`disconnect()` 已经把状态清干净）。
    // 这时不能再把地址写回去 —— 否则会给一台离线的设备挂上一个流，
    // 画面区域变成"离线蒙版盖着一条没人看的连接"。
    if (!online.value) return
    // 新地址（stamp 递增）会让浏览器当成一次全新的请求，不吃缓存
    src.value = streamUrl(props.device.id, ticket, ++stamp)
    streamError.value = null
    armGrace()
  } catch (err) {
    // **不弹全局提示**：换票失败多半是设备被解绑或后端短暂不可用，
    // 弹窗会在自动重试的每一轮都弹一次。原因就地显示在画面上。
    //
    // 只在**还没画出画面**时才铺蒙版：90 秒一次的那种换票失败时，旧连接
    // 通常还在正常出图，此时显示"画面中断"是在说假话。持续失败也不会被瞒住
    // —— 旧票据 120 秒到期后流出错，`@error` 会把它如实报出来，而且下面的
    // 重试一直在跑。
    if (!firstFrame.value) streamError.value = normalizeError(err).message
    scheduleRetry()
  } finally {
    inFlight = false
  }
}

/** 第一帧迟迟不来时把原因说清楚，而不是无限转圈 */
function armGrace(): void {
  if (firstFrame.value || graceTimer !== null) return
  graceTimer = window.setTimeout(() => {
    graceTimer = null
    waitedTooLong.value = true
  }, GRACE_MS)
}

function scheduleRetry(): void {
  if (retryTimer !== null) return
  retryTimer = window.setTimeout(() => {
    retryTimer = null
    void connect()
  }, RETRY_DELAY_MS)
}

/**
 * 断开（设备离线时）。
 * **不动 `refreshTimer`** —— 那是页面级的，见文件头。
 */
function disconnect(): void {
  if (retryTimer !== null) {
    window.clearTimeout(retryTimer)
    retryTimer = null
  }
  if (graceTimer !== null) {
    window.clearTimeout(graceTimer)
    graceTimer = null
  }
  src.value = ''
  firstFrame.value = false
  waitedTooLong.value = false
  streamError.value = null
}

function onLoad(): void {
  firstFrame.value = true
  waitedTooLong.value = false
  if (graceTimer !== null) {
    window.clearTimeout(graceTimer)
    graceTimer = null
  }
}

function onError(): void {
  // 不清 `src`：让 `<img>` 留着上一帧（蒙版会盖住它，见文件头）
  if (!streamError.value) streamError.value = '画面已中断，正在重新连接…'
  scheduleRetry()
}

watch(online, (isOnline) => {
  // 只有开了看的连接才需要跟着在线状态接上/放下；没开看时设备上下线与画面无关
  if (!started.value) return
  if (isOnline) void connect()
  else disconnect()
})

watch(started, (open) => {
  if (open) {
    if (online.value) void connect()
  } else {
    // 手动断开：把连接相关的状态清干净，回到"待查看"的样子
    disconnect()
  }
})

onMounted(() => {
  // 不自动连：等用户点「查看摄像头」（started 变 true 时 watch 会去连）
  refreshTimer = window.setInterval(() => {
    // 离线或没开看时这里什么也不做 —— 定时器本身不随离线被清掉，
    // 设备回来/重新开看后才能继续换票
    if (started.value && props.device.online) void connect()
  }, TICKET_REFRESH_MS)
})

onBeforeUnmount(() => {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer)
    refreshTimer = null
  }
  disconnect()
})
</script>

<template>
  <section class="live ip-panel">
    <header class="live__head">
      <h3 class="ip-section-title">实时画面</h3>
      <span class="live__state">
        <template v-if="started">
          <ElButton
            size="small"
            text
            type="danger"
            @click="started = false"
          >
            断开画面
          </ElButton>
          <span class="ip-dot" :class="online ? 'ip-dot--ok' : 'ip-dot--off'" aria-hidden="true" />
          <span class="num">{{
            online ? `推流 ${device.stream_fps.toFixed(1)} 帧/秒` : '设备离线'
          }}</span>
        </template>
        <template v-else>
          <span class="ip-dot" :class="online ? 'ip-dot--ok' : 'ip-dot--off'" aria-hidden="true" />
          <span class="num">{{ online ? '设备在线' : '设备离线' }}</span>
        </template>
      </span>
    </header>

    <div class="live__stage">
      <img
        v-if="src"
        class="live__img"
        :src="src"
        :alt="`${device.name || '设备'} 的实时摄像头画面`"
        @load="onLoad"
        @error="onError"
      />

      <!--
        蒙版：没开看、离线、出错、还没有第一帧时都要盖住。
        "还没有第一帧"这一条不能省 —— 否则一帧冻住的旧画面会被当成直播。
      -->
      <div v-if="!started || !online || streamError || waiting" class="live__veil">
        <p class="live__veil-title">
          <template v-if="!started && online">摄像头未开启</template>
          <template v-else-if="streamError">画面中断</template>
          <template v-else-if="!online">设备离线</template>
          <template v-else>正在连接画面…</template>
        </p>
        <p v-if="!started && online" class="live__veil-hint">
          画面不会自动播放。点击「查看摄像头」开始接收这台盒子的实时视频流，
          不想看时点「断开画面」即可，不影响盒子端的抓拍与上报。
        </p>
        <p v-else-if="!online && !streamError" class="live__veil-hint">
          最近 60 秒没有收到这台盒子的任何消息。盒子断电、没网、或后端没起来时都会这样。
        </p>
        <p v-else-if="coverText" class="live__veil-hint">{{ coverText }}</p>
        <ElButton
          v-if="!started && online"
          size="small"
          type="primary"
          @click="started = true"
        >
          查看摄像头
        </ElButton>
        <ElButton
          v-if="started && online && waitedTooLong"
          size="small"
          text
          type="primary"
          @click="connect"
        >
          重试
        </ElButton>
      </div>
    </div>

    <p class="live__foot ip-dim">
      <template v-if="started && online && streaming">
        现场画面按 <span class="num">{{ device.stream_fps.toFixed(1) }}</span> 帧/秒持续推送到中心，
        网页只是转播它。若盒子在<strong>本机「系统设置」</strong>开启了实时识别，这一路会带检测框，
        帧率也会被限制在 <span class="num">2</span> 帧/秒；未开启时是原始画面。
        检测结果看下方。
      </template>
      <template v-else-if="!started">
        摄像头画面按需查看：点击上方「查看摄像头」开始播放，
        断开后盒子照常定时抓拍并把识别结果上报到中心。
      </template>
      <template v-else>画面通道就绪后会在这里直接播放，无需刷新页面。</template>
    </p>
  </section>
</template>

<style scoped lang="scss">
.live {
  padding: var(--ip-gap);

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 10px;
  }

  &__state {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__stage {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    aspect-ratio: 16 / 9;
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius);
    background: #0b0f0d;
    overflow: hidden;
  }

  /* contain 而不是 cover：宁可留黑边，也不要把画面裁掉一半 ——
     田里的画面裁掉哪一边都可能正好是虫子那一侧 */
  &__img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
  }

  &__veil {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 16px;
    background: rgba(8, 12, 10, 0.86);
    text-align: center;
  }

  &__veil-title {
    margin: 0;
    font-size: var(--ip-fs-body);
    color: var(--ip-text);
  }

  &__veil-hint {
    margin: 0;
    max-width: 46ch;
    font-size: var(--ip-fs-xs);
    line-height: 1.7;
    color: var(--ip-text-3);
  }

  &__foot {
    margin: 8px 0 0;
    font-size: var(--ip-fs-xs);
    line-height: 1.7;
  }
}
</style>
