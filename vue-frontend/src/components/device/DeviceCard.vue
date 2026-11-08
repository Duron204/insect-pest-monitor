<script setup lang="ts">
/**
 * 设备列表里的一台盒子。
 *
 * 整张卡是一个按钮（跳到详情页），所以卡里**不放**任何别的可点元素 ——
 * 嵌套按钮在键盘操作与读屏下都很糟。解绑这类动作放在详情页，那里有确认框。
 *
 * `online` 用的是后端派生的字段，不在这里按 `last_seen_at` 重算
 * （理由见 stores/devices.ts 的文件头）。
 */
import { computed } from 'vue'
import { formatRelative } from '@/utils'
import type { DeviceOut } from '@/types'

const props = defineProps<{ device: DeviceOut }>()
defineEmits<{ (event: 'open', device: DeviceOut): void }>()

const online = computed(() => props.device.online)

/** 没起名字就退回硬件标识的尾部（一整串太占地方，尾部已足够区分盒子） */
const title = computed(() => {
  const name = props.device.name?.trim()
  if (name) return name
  const id = props.device.hardware_id
  return id.length > 12 ? `…${id.slice(-8)}` : id
})

const lastSeen = computed(() =>
  props.device.last_seen_at ? formatRelative(props.device.last_seen_at) : '从未连上'
)

const lastTask = computed(() =>
  props.device.last_task_at ? formatRelative(props.device.last_task_at) : '还没上报过'
)

/** 帧率只在真的在推的时候显示：0 表示"没在推"，摆一个 0.0 帧会让人以为卡了 */
const streaming = computed(() => props.device.stream_fps > 0)
</script>

<template>
  <button
    class="device-card"
    type="button"
    :aria-label="`查看设备 ${title} 的详情`"
    @click="$emit('open', device)"
  >
    <span class="device-card__head">
      <span class="ip-dot" :class="online ? 'ip-dot--ok' : 'ip-dot--off'" aria-hidden="true" />
      <span class="device-card__name ip-truncate">{{ title }}</span>
      <span class="device-card__state" :class="{ 'is-off': !online }">
        {{ online ? '在线' : '离线' }}
      </span>
    </span>

    <span class="device-card__model ip-truncate">
      {{ device.model || '型号未上报' }}
      <template v-if="device.agent_version"> · 代理 v{{ device.agent_version }}</template>
    </span>

    <span class="device-card__meta">
      <span class="device-card__row">
        <span class="ip-muted">最近心跳</span>
        <span class="num">{{ lastSeen }}</span>
      </span>
      <span class="device-card__row">
        <span class="ip-muted">最近上报</span>
        <span class="num">{{ lastTask }}</span>
      </span>
    </span>

    <span class="device-card__foot">
      <span v-if="streaming" class="ip-chip ip-chip--primary">
        {{ device.stream_fps.toFixed(1) }} 帧/秒
      </span>
      <span v-else class="ip-chip">未推流</span>
      <span class="device-card__hw num ip-dim">{{ device.hardware_id }}</span>
    </span>
  </button>
</template>

<style scoped lang="scss">
.device-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  padding: 12px 14px;
  background: var(--ip-panel);
  border: 1px solid var(--ip-line);
  border-radius: var(--ip-radius);
  box-shadow: var(--ip-inner);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--ip-dur-fast) var(--ip-ease),
    background var(--ip-dur-fast) var(--ip-ease);

  &:hover {
    border-color: var(--ip-line-strong);
    background: var(--ip-panel-2);
  }

  &:focus-visible {
    outline: 2px solid var(--ip-primary);
    outline-offset: 2px;
  }

  &__head {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  &__name {
    flex: 1 1 auto;
    font-size: var(--ip-fs-body);
    font-weight: 600;
    color: var(--ip-text);
  }

  &__state {
    flex: 0 0 auto;
    font-size: var(--ip-fs-xs);
    color: var(--ip-primary);

    &.is-off {
      color: var(--ip-text-3);
    }
  }

  &__model {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__meta {
    display: grid;
    gap: 2px;
  }

  &__row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
    font-size: var(--ip-fs-xs);
  }

  &__foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: 2px;
  }

  /* 硬件标识是排查用的，最长；单独一行截断，不挤掉别的字段 */
  &__hw {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
</style>
