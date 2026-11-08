<script setup lang="ts">
/**
 * 绑定设备 —— 输 6 位配对码。
 *
 * 码是**盒子自己生成并显示**的（中心只负责核对），所以这个弹窗只做一件事：
 * 把用户看到的码原样交给后端。这里不生成码、也不猜码。
 *
 * 输入框只收数字：后端 `_normalize_code` 会丢掉非数字字符（用户常把
 * 「381 402」连空格一起粘进来），前端先做一遍同样的过滤，用户就不会遇到
 * "看着填对了却按不动按钮"的情况。
 */
import { computed, ref, watch } from 'vue'
import { ElButton, ElDialog, ElInput } from 'element-plus'
import { useDevicesStore } from '@/stores/devices'
import type { DeviceOut } from '@/types'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
  (event: 'bound', device: DeviceOut): void
}>()

const store = useDevicesStore()
const code = ref('')

const CODE_LENGTH = 6

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value)
})

const ready = computed(() => code.value.length === CODE_LENGTH)

/** 只留数字，并且**截断在 6 位**：粘贴整段日志时不会把后面的东西也带进去。 */
function sanitize(value: string): string {
  return value.replace(/\D/g, '').slice(0, CODE_LENGTH)
}

function onInput(value: string): void {
  code.value = sanitize(value)
}

async function submit(): Promise<void> {
  if (!ready.value || store.busy) return
  const device = await store.bind(code.value)
  // 失败时（码错/过期/已被绑定/尝试过多）**不关弹窗**：后端给的中文原因
  // 就显示在输入框下方，用户可以直接改一个码再试。
  if (!device) return
  emit('bound', device)
  visible.value = false
}

// 每次打开都清空上一次的输入与残留的报错，避免"上次的错码还留着"导致的误判
watch(visible, (open) => {
  if (!open) return
  code.value = ''
  store.clearError()
})
</script>

<template>
  <ElDialog
    v-model="visible"
    title="绑定树莓派设备"
    width="440px"
    append-to-body
    :close-on-click-modal="!store.busy"
  >
    <div class="pairing">
      <p class="pairing__lead">
        在<strong>树莓派的终端</strong>里会显示一个 6 位配对码，把它填到这里。
      </p>
      <ul class="pairing__where">
        <li>用 systemd 部署时，在盒子上执行 <code>journalctl -u insect-backend -f</code></li>
        <li>同一份码也写在盒子数据目录的 <code>agent_pairing.txt</code> 里</li>
      </ul>

      <ElInput
        :model-value="code"
        class="pairing__input"
        maxlength="6"
        inputmode="numeric"
        autocomplete="one-time-code"
        placeholder="6 位数字"
        size="large"
        :disabled="store.busy"
        aria-label="配对码"
        @input="onInput"
        @keyup.enter="submit"
      />

      <!-- 后端的三条中文原因（码无效/已过期、已被绑定、尝试次数过多）直接显示，
           不在这里另写一套文案，免得两边不一致 -->
      <p v-if="store.error" class="pairing__error" role="alert">{{ store.error }}</p>
      <p v-else class="pairing__hint">
        配对码 5 分钟内有效。绑定成功后盒子会自动拿到访问令牌，无需再操作盒子。
      </p>
    </div>

    <template #footer>
      <ElButton :disabled="store.busy" @click="visible = false">取消</ElButton>
      <ElButton type="primary" :loading="store.busy" :disabled="!ready" @click="submit">
        绑定
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped lang="scss">
.pairing {
  display: flex;
  flex-direction: column;
  gap: 10px;

  &__lead {
    margin: 0;
    font-size: var(--ip-fs-sm);
    line-height: 1.7;
    color: var(--ip-text-2);
  }

  &__where {
    margin: 0;
    padding-left: 18px;
    font-size: var(--ip-fs-xs);
    line-height: 1.9;
    color: var(--ip-text-3);

    code {
      padding: 1px 5px;
      border-radius: 4px;
      background: var(--ip-panel-2);
      color: var(--ip-text-2);
      font-family: var(--ip-font-mono, monospace);
    }
  }

  /* 码是给眼睛抄的，放大、拉开字距、等宽，读错的概率低一些 */
  &__input {
    :deep(.el-input__inner) {
      font-size: 22px;
      letter-spacing: 0.34em;
      text-align: center;
      font-variant-numeric: tabular-nums;
    }
  }

  &__error {
    margin: 0;
    font-size: var(--ip-fs-sm);
    color: var(--ip-danger);
  }

  &__hint {
    margin: 0;
    font-size: var(--ip-fs-xs);
    line-height: 1.7;
    color: var(--ip-text-3);
  }
}
</style>
