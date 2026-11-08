<script setup lang="ts">
/**
 * 我的设备 —— 挂在当前账号下的树莓派盒子。
 *
 * 与「设备状态」页的分工（两页名字像，含义完全不同，页面上都要说清楚）：
 *   * 本页 = **田里的盒子**。一台盒子在田里，中心在别处，画面与结果由盒子推过来。
 *   * 设备状态 = **跑后端这台机器**的健康。它跟当前打开网页的电脑也可能不是同一台。
 *
 * 盒子是**只外拨**的：它主动连中心，中心从不连它。所以这里不需要任何端口映射、
 * 内网穿透之类的设置 —— 盒子上把 `agent.center_url` 填成这里的地址就够了。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton } from 'element-plus'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import DeviceCard from '@/components/device/DeviceCard.vue'
import PairingDialog from '@/components/device/PairingDialog.vue'
import { useDevicesStore } from '@/stores/devices'
import type { DeviceOut, DevicePendingOut } from '@/types'

const router = useRouter()
const store = useDevicesStore()

const pairing = ref(false)

/**
 * 在线状态变化最快也要一个心跳周期（10 秒），10 秒轮一次足够；
 * 与「设备状态」页同一节奏，别再快 —— 每次都是一次真实请求。
 */
const POLL_MS = 10_000
let pollTimer: number | null = null

const subtitle = computed(() => {
  if (!store.count) return '还没有绑定任何设备'
  return `已绑定 ${store.count} 台 · ${store.onlineCount} 台在线`
})

/** 正在敲门、且配对码还没过期的盒子。过期那条后端已经不返回了，这里再挡一层 */
const knocking = computed<DevicePendingOut[]>(() =>
  store.pending.filter((box) => new Date(box.pairing_expires_at).getTime() > now.value)
)

/** 取最快过期的那台算倒计时——人要赶的就是这一台 */
const knockDeadline = computed(() => {
  if (!knocking.value.length) return '配对码即将过期'
  const soonest = Math.min(
    ...knocking.value.map((box) => new Date(box.pairing_expires_at).getTime())
  )
  const minutes = Math.max(1, Math.round((soonest - now.value) / 60_000))
  return `配对码约 ${minutes} 分钟内有效`
})

const knockNames = computed(() =>
  knocking.value.map((box) => box.name?.trim() || `盒子 ${box.id}`).join('、')
)

/** 当前时刻（每秒刷新一次），驱动倒计时与过期剔除 */
const now = ref(Date.now())
// 倒计时与过期剔除都要跟着时间走。1 秒一次只驱动一个字符串的 computed，
// 代价可以忽略；换来的是"码什么时候失效"这件事不用刷新页面就知道。
let clockTimer: number | null = null

function openDevice(device: DeviceOut): void {
  void router.push({ name: 'device-detail', params: { id: String(device.id) } })
}

async function onBound(device: DeviceOut): Promise<void> {
  // 绑定成功直接进详情页：那里才有画面和结果，留在列表页什么也看不到
  await router.push({ name: 'device-detail', params: { id: String(device.id) } })
}

onMounted(async () => {
  await store.loadDevices(true)
  pollTimer = window.setInterval(() => void store.loadDevices(true), POLL_MS)
  clockTimer = window.setInterval(() => {
    now.value = Date.now()
  }, 1_000)
})

onBeforeUnmount(() => {
  for (const timer of [pollTimer, clockTimer]) {
    if (timer !== null) window.clearInterval(timer)
  }
  pollTimer = null
  clockTimer = null
})
</script>

<template>
  <div class="devices">
    <header class="devices__bar ip-panel">
      <div class="devices__identity">
        <div>
          <h2 class="ip-section-title">我的设备</h2>
          <p class="ip-page-sub">{{ subtitle }}</p>
        </div>
      </div>
      <div class="devices__actions">
        <ElButton size="small" :loading="store.loading" @click="store.loadDevices(true)">
          刷新
        </ElButton>
        <ElButton size="small" type="primary" @click="pairing = true">绑定设备</ElButton>
      </div>
    </header>

    <ErrorBanner :message="store.error" title="设备列表加载失败" @close="store.clearError()" />

    <!-- 有盒子在敲门：人不用去盒子的终端才知道配对开始了 -->
    <section v-if="knocking.length" class="devices__knock ip-panel">
      <span class="devices__knock-dot" aria-hidden="true" />
      <div class="devices__knock-text">
        <strong>有 {{ knocking.length }} 台盒子正在等待绑定</strong>
        <span v-if="knockNames">：{{ knockNames }}</span>
        <p class="devices__knock-hint">
          {{ knockDeadline }}。在<strong>盒子侧</strong>（它的终端、或它的本地网页）
          找到那 6 位配对码，填进这里即可完成绑定。
        </p>
      </div>
      <ElButton size="small" type="primary" @click="pairing = true">输入配对码</ElButton>
    </section>

    <p class="devices__note ip-panel">
      这里管的是<strong>田里的树莓派</strong>：盒子主动连到本机，所以不需要给盒子做端口映射或内网穿透，
      只要在盒子的配置里把 <code>agent.center_url</code> 填成本机地址并打开 <code>agent.enabled</code>。
      想看<strong>跑后端的这台机器</strong>的温度与磁盘，去「设备状态」页。
    </p>

    <section v-if="store.count" class="devices__grid">
      <DeviceCard
        v-for="device in store.items"
        :key="device.id"
        :device="device"
        @open="openDevice"
      />
    </section>

    <EmptyState
      v-else-if="!store.loading"
      description="还没有绑定设备"
      hint="在树莓派上打开 agent.enabled 并启动后端，盒子的终端会显示一个 6 位配对码；点「绑定设备」把码填进来即可。"
      action-text="绑定设备"
      @action="pairing = true"
    />

    <section v-else class="devices__loading ip-panel">
      <p class="ip-dim">正在加载设备列表…</p>
    </section>

    <PairingDialog v-model="pairing" @bound="onBound" />
  </div>
</template>

<style scoped lang="scss">
.devices {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;

  &__bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 12px;
  }

  &__identity {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  &__actions {
    display: flex;
    gap: 8px;
  }

  &__knock {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 12px;
    font-size: var(--ip-fs-sm);
    line-height: 1.7;
    color: var(--ip-text);
    border-left: 3px solid var(--ip-warn);
  }

  &__knock-dot {
    flex: 0 0 auto;
    width: 8px;
    height: 8px;
    margin-top: 8px;
    border-radius: 50%;
    background: var(--ip-warn);
  }

  &__knock-text {
    flex: 1 1 auto;
    min-width: 0;
  }

  &__knock-hint {
    margin: 4px 0 0;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__note {
    margin: 0;
    padding: 10px 12px;
    font-size: var(--ip-fs-sm);
    line-height: 1.7;
    color: var(--ip-text-2);

    code {
      padding: 1px 5px;
      border-radius: 4px;
      background: var(--ip-panel-2);
      font-family: var(--ip-font-mono, monospace);
      color: var(--ip-text);
    }
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(258px, 1fr));
    gap: 10px;
  }

  &__loading {
    padding: var(--ip-gap);
    text-align: center;
  }
}
</style>
