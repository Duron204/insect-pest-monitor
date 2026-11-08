<script setup lang="ts">
/**
 * 设备详情 —— 一台盒子的实时画面 + 检测结果 + 改名/解绑。
 *
 * 两件必须做对的事：
 *   1. **404 要能区分出来**：`GET /api/devices/{id}` 对别人的设备和已解绑的设备
 *      都回 404（刻意不区分，不让外部确认"这个 id 存在"）。所以出现 404 时
 *      页面要停下轮询并说清"不存在、或已解绑/不属于本账号"，
 *      而不是继续拿缓存里的旧对象渲染一个已经不存在的设备。
 *   2. **轮询只为刷新在线与帧率**：这两个值会变，名字和硬件标识不会。
 *      10 秒一次与「设备状态」页同节奏。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton, ElInput } from 'element-plus'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import LiveStreamPanel from '@/components/device/LiveStreamPanel.vue'
import DeviceResults from '@/components/device/DeviceResults.vue'
import DeviceDiagnostics from '@/components/device/DeviceDiagnostics.vue'
import { normalizeError } from '@/api'
import { getDevice } from '@/api/devices'
import { useDevicesStore } from '@/stores/devices'
import { formatDateTime } from '@/utils'
import { confirmAction, notifyApiError } from '@/utils/notify'
import type { DeviceOut } from '@/types'

const route = useRoute()
const router = useRouter()
const store = useDevicesStore()

const POLL_MS = 10_000

const deviceId = computed(() => {
  const raw = Array.isArray(route.params.id) ? route.params.id[0] : route.params.id
  const parsed = Number.parseInt(String(raw ?? ''), 10)
  return Number.isFinite(parsed) ? parsed : null
})

const device = ref<DeviceOut | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
/** 设备不存在 / 已解绑 / 不属于本账号 —— 三种情况后端都回 404，这里一并处理 */
const gone = ref(false)

const editingName = ref(false)
const nameDraft = ref('')

let pollTimer: number | null = null

const title = computed(() => device.value?.name?.trim() || '未命名设备')

async function load(): Promise<void> {
  if (deviceId.value === null) {
    gone.value = true
    return
  }
  loading.value = true
  try {
    const fresh = await getDevice(deviceId.value)
    device.value = fresh
    store.upsert(fresh)
    gone.value = false
    error.value = null
  } catch (err) {
    if (normalizeError(err).status === 404) {
      // 停下来，并且**清掉手上这份**：继续拿旧对象渲染会让人以为盒子还在
      gone.value = true
      device.value = null
      stopPoll()
      return
    }
    error.value = notifyApiError(err)
  } finally {
    loading.value = false
  }
}

function stopPoll(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

function startEdit(): void {
  nameDraft.value = device.value?.name ?? ''
  editingName.value = true
}

async function saveName(): Promise<void> {
  if (deviceId.value === null) return
  if (await store.rename(deviceId.value, nameDraft.value)) {
    editingName.value = false
    await load()
  }
}

async function onUnbind(): Promise<void> {
  if (!device.value || deviceId.value === null) return
  const ok = await confirmAction({
    title: '解绑设备',
    message:
      `确认解绑「${title.value}」？\n` +
      '盒子会失去访问权限，并在下一次向中心签到后重新显示配对码。\n' +
      '已上报的历史结果会保留，只是不再归属这台设备。',
    confirmText: '解绑',
    danger: true
  })
  if (!ok) return
  if (await store.unbind(deviceId.value)) await router.push({ name: 'devices' })
}

onMounted(async () => {
  // 先用列表里已有的那份立刻渲染（避免白屏），再拉一次拿最新的在线状态
  const cached = deviceId.value === null ? null : store.byId(deviceId.value)
  if (cached) device.value = cached
  await load()
  if (gone.value) return
  pollTimer = window.setInterval(() => void load(), POLL_MS)
})

onBeforeUnmount(stopPoll)
</script>

<template>
  <div class="detail">
    <header class="detail__bar ip-panel">
      <div class="detail__identity">
        <ElButton text size="small" @click="router.push({ name: 'devices' })">← 我的设备</ElButton>
        <span class="ip-dot" :class="device?.online ? 'ip-dot--ok' : 'ip-dot--off'" aria-hidden="true" />
        <div class="detail__titles">
          <template v-if="editingName">
            <div class="detail__rename">
              <ElInput
                v-model="nameDraft"
                size="small"
                maxlength="96"
                placeholder="设备名称"
                aria-label="设备名称"
                @keyup.enter="saveName"
                @keyup.esc="editingName = false"
              />
              <ElButton size="small" type="primary" :loading="store.busy" @click="saveName">
                保存
              </ElButton>
              <ElButton size="small" :disabled="store.busy" @click="editingName = false">
                取消
              </ElButton>
            </div>
          </template>
          <template v-else>
            <h2 class="ip-section-title">
              {{ title }}
              <ElButton text size="small" type="primary" @click="startEdit">改名</ElButton>
            </h2>
            <p class="ip-page-sub">
              {{ device?.online ? '在线' : '离线' }}
              <template v-if="device?.model"> · <span class="num">{{ device.model }}</span></template>
              <template v-if="device"> · <span class="num">{{ device.hardware_id }}</span></template>
            </p>
          </template>
        </div>
      </div>
      <ElButton
        v-if="device"
        size="small"
        type="danger"
        plain
        :loading="store.busy"
        @click="onUnbind"
      >
        解绑
      </ElButton>
    </header>

    <ErrorBanner :message="error" title="设备信息加载失败" @close="error = null" />

    <template v-if="gone">
      <EmptyState
        description="设备不存在"
        hint="这台设备可能已经解绑、或者不属于当前账号。刷新列表看看它是否还在。"
        action-text="返回我的设备"
        @action="router.push({ name: 'devices' })"
      />
    </template>

    <template v-else-if="device">
      <LiveStreamPanel :device="device" />
      <DeviceResults :device="device" />
      <DeviceDiagnostics :device="device" />

      <p class="detail__foot ip-dim">
        数据来源：GET /api/devices/{{ device.id }}（在线与帧率）、
        GET /api/devices/{{ device.id }}/stream（实时画面）、
        GET /api/stats/summary?device_id={{ device.id }} 与
        GET /api/devices/{{ device.id }}/tasks（检测结果）。
        <template v-if="device.bound_at">
          绑定于 <span class="num">{{ formatDateTime(device.bound_at) }}</span>。
        </template>
      </p>
    </template>

    <section v-else class="detail__loading ip-panel">
      <p class="ip-dim">正在加载设备信息…</p>
    </section>
  </div>
</template>

<style scoped lang="scss">
.detail {
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

  &__titles {
    min-width: 0;
  }

  &__rename {
    display: flex;
    align-items: center;
    gap: 6px;
    max-width: 420px;
  }

  &__loading {
    padding: var(--ip-gap);
    text-align: center;
  }

  &__foot {
    margin: 0;
    font-size: var(--ip-fs-xs);
    line-height: 1.7;
  }
}
</style>
