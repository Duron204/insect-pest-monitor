<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ElAvatar,
  ElButton,
  ElDescriptions,
  ElDescriptionsItem,
  ElForm,
  ElFormItem,
  ElInput,
  ElProgress,
  ElTag
} from 'element-plus'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import LiveDetectionPanel from '@/components/detection/LiveDetectionPanel.vue'
import LocalCameraPanel from '@/components/detection/LocalCameraPanel.vue'
import { useModelsStore } from '@/stores/models'
import { useUiStore } from '@/stores/ui'
import { useUserStore } from '@/stores/user'
import { formatDateTime, formatNumber } from '@/utils'
import { confirmAction, notifyApiError, notifySuccess, notifyWarning } from '@/utils/notify'

const user = useUserStore()
const models = useModelsStore()
const ui = useUiStore()
const router = useRouter()

const avatarInput = ref<HTMLInputElement | null>(null)
const avatarUploading = ref(false)
const avatarPercent = ref(0)
const avatarError = ref<string | null>(null)

const passwordForm = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' })
const passwordError = ref<string | null>(null)
const passwordSubmitting = ref(false)

const healthLoading = computed(() => models.healthLoading)
const health = computed(() => models.health)

const deviceText = computed(() => {
  const data = health.value
  if (!data) return '—'
  return `${data.device}${data.gpu_available ? '（GPU 可用）' : '（CPU）'}`
})

const modelStatusText = computed(() => {
  const data = health.value
  if (!data) return '未知'
  return data.model_loaded ? '已加载' : '未加载'
})

/**
 * 后端 `model_names` 是**类别数**（数字），不是类别名数组。
 * 之前这里写成 `?? []` 并到处用 `.length`，于是：
 *   - 上面"类别数"一栏永远显示 undefined
 *   - 模型明明加载了，下面却提示"后端未返回类别列表"
 */
const classCount = computed(() => health.value?.model_names ?? 0)

/** 后端回退原因：非空说明配置的推理后端没生效，实际回退到了 torch。 */
const fallbackReason = computed(() => health.value?.fallback_reason ?? null)

/** health 的局部错误（避免直接写 store 的 ref） */
const localHealthError = ref<string | null>(null)

function clearHealthError(): void {
  localHealthError.value = null
}

function openAvatarPicker(): void {
  avatarInput.value?.click()
}

async function onAvatarPicked(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  const file = files[0]
  if (!file) return
  if (!file.type.startsWith('image/')) {
    notifyWarning('头像必须是图片文件')
    return
  }
  avatarUploading.value = true
  avatarPercent.value = 0
  avatarError.value = null
  try {
    await user.updateAvatar(file)
    notifySuccess('头像已更新')
  } catch (err) {
    avatarError.value = notifyApiError(err, false)
  } finally {
    avatarUploading.value = false
    avatarPercent.value = 0
  }
}

function resetPasswordForm(): void {
  passwordForm.oldPassword = ''
  passwordForm.newPassword = ''
  passwordForm.confirmPassword = ''
}

async function onChangePassword(): Promise<void> {
  passwordError.value = null
  if (passwordForm.oldPassword.length === 0) {
    passwordError.value = '请输入当前密码'
    return
  }
  if (passwordForm.newPassword.length < 6) {
    passwordError.value = '新密码至少 6 位'
    return
  }
  if (passwordForm.newPassword !== passwordForm.confirmPassword) {
    passwordError.value = '两次输入的新密码不一致'
    return
  }
  passwordSubmitting.value = true
  try {
    const message = await user.updatePassword(passwordForm.oldPassword, passwordForm.newPassword)
    notifySuccess(message || '密码已更新')
    resetPasswordForm()
  } catch (err) {
    passwordError.value = notifyApiError(err, false)
  } finally {
    passwordSubmitting.value = false
  }
}

async function onLogout(): Promise<void> {
  const ok = await confirmAction({
    title: '退出登录',
    message: '确认退出当前账号？',
    confirmText: '退出',
    danger: true
  })
  if (!ok) return
  await user.logout()
  notifySuccess('已退出登录')
  await router.replace({ name: 'login' })
}

async function refreshHealth(): Promise<void> {
  localHealthError.value = null
  await models.loadHealth()
  if (models.healthError) localHealthError.value = models.healthError
  await models.loadRegistry(true)
}

onMounted(async () => {
  await refreshHealth()
  if (user.isAuthenticated) await user.fetchMe()
})
</script>

<template>
  <div class="settings-view">
    <input
      ref="avatarInput"
      class="settings-view__input"
      type="file"
      accept="image/*"
      aria-label="选择头像图片"
      @change="onAvatarPicked"
    >

    <section class="settings-view__card ip-panel" aria-label="当前用户">
      <h2 class="ip-section-title">当前用户</h2>
      <div class="settings-view__user">
        <ElAvatar :size="56" :src="user.avatarUrl || undefined">
          {{ user.initial }}
        </ElAvatar>
        <div class="settings-view__user-info">
          <p class="settings-view__username">{{ user.displayName }}</p>
          <p class="settings-view__meta">
            账号 ID <span class="num">{{ formatNumber(user.user?.id ?? null) }}</span>
            · 创建于 {{ formatDateTime(user.user?.created_at) }}
          </p>
          <p class="settings-view__meta">
            最近登录 {{ formatDateTime(user.user?.last_login_at) }}
          </p>
          <div class="settings-view__tags">
            <ElTag v-if="user.mustResetPassword" size="small" type="warning" effect="plain">
              后端要求重置密码
            </ElTag>
            <ElTag size="small" effect="plain">
              {{ ui.theme === 'dark' ? '深色主题' : '浅色主题' }}
            </ElTag>
          </div>
        </div>
        <div class="settings-view__user-actions">
          <ElButton size="small" :loading="avatarUploading" aria-label="上传头像" @click="openAvatarPicker">
            上传头像
          </ElButton>
          <ElButton size="small" text type="danger" aria-label="退出登录" @click="onLogout">
            退出登录
          </ElButton>
        </div>
      </div>
      <div v-if="avatarUploading" class="settings-view__upload">
        <ElProgress :percentage="avatarPercent" :stroke-width="5" :show-text="false" />
        <span class="num settings-view__upload-percent">{{ avatarPercent }}%</span>
      </div>
      <ErrorBanner :message="avatarError" title="头像更新失败" @close="avatarError = null" />
    </section>

    <section class="settings-view__card ip-panel" aria-label="后端与设备信息">
      <header class="settings-view__card-head">
        <h2 class="ip-section-title">后端与设备</h2>
        <div class="settings-view__card-actions">
          <span class="settings-view__status">
            <span
              class="ip-dot"
              :class="models.backendOnline ? (models.modelLoaded ? 'ip-dot--ok' : 'ip-dot--warn') : 'ip-dot--err'"
              aria-hidden="true"
            />
            {{ models.backendOnline ? '后端在线' : '后端离线' }}
          </span>
          <ElButton size="small" :loading="healthLoading" aria-label="刷新后端状态" @click="refreshHealth">
            刷新
          </ElButton>
        </div>
      </header>

      <ErrorBanner
        v-if="localHealthError"
        :message="localHealthError"
        title="无法读取 /api/health"
        @close="clearHealthError"
      />

      <ElDescriptions :column="2" border size="small">
        <ElDescriptionsItem label="服务状态">
          {{ health?.status ?? '—' }}
        </ElDescriptionsItem>
        <ElDescriptionsItem label="版本">
          <span class="num">{{ health?.version ?? '—' }}</span>
        </ElDescriptionsItem>
        <ElDescriptionsItem label="推理设备">{{ deviceText }}</ElDescriptionsItem>
        <ElDescriptionsItem label="推理后端">
          <span class="num">{{ health?.backend ?? '—' }}</span>
        </ElDescriptionsItem>
        <ElDescriptionsItem label="模型是否加载">
          <ElTag size="small" effect="plain" :type="models.modelLoaded ? 'success' : 'warning'">
            {{ modelStatusText }}
          </ElTag>
        </ElDescriptionsItem>
        <ElDescriptionsItem label="类别数">
          <span class="num">{{ classCount }}</span>
        </ElDescriptionsItem>
        <ElDescriptionsItem label="模型路径" :span="2">
          <span class="num settings-view__path">{{ health?.model_path ?? '—' }}</span>
        </ElDescriptionsItem>
        <ElDescriptionsItem label="当前选中模型" :span="2">
          {{ models.currentName }}
        </ElDescriptionsItem>
      </ElDescriptions>

      <div class="settings-view__classes">
        <p class="ip-label">模型类别数（model_names）</p>
        <p v-if="classCount > 0" class="settings-view__class-count">
          当前权重共 <span class="num">{{ classCount }}</span> 类
        </p>
        <p v-else class="settings-view__empty-note">后端未返回类别数（模型可能尚未加载）</p>
        <!--
          部署到边缘设备时这一条最关键：配置里写了 onnx 却回退到 torch，
          只看配置文件是发现不了的，必须靠后端如实上报的原因。
        -->
        <p v-if="fallbackReason" class="settings-view__warn">
          推理后端发生回退：{{ fallbackReason }}
          <br />
          请检查 inference.backend 与对应依赖（例如 onnxruntime）是否已安装。
        </p>
      </div>

      <p class="settings-view__note">
        说明：后端已提供地块接口（<code>/api/plots</code>），但<b>地块管理界面</b>尚未实现 ——
        目前地块只用于任务归属与统计计数。采集节点与设备管理已随“只用树莓派本机摄像头”整体移除，
        不再属于待办项。
      </p>
    </section>

    <!-- 边缘盒子自己插着的摄像头：状态、手动抓拍与排障都在这一块 -->
    <LocalCameraPanel />

    <!--
      推流画面的实时识别开关。只在 agent.enabled=true 的机器（现场＝树莓派）
      上渲染；中心自己的设置页看不到它 —— 中心无法反向控制盒子，这是设计约束。
    -->
    <LiveDetectionPanel />

    <section class="settings-view__card ip-panel" aria-label="修改密码">
      <h2 class="ip-section-title">修改密码</h2>
      <ElForm label-position="top" class="settings-view__form" @submit.prevent="onChangePassword">
        <ElFormItem label="当前密码">
          <ElInput
            v-model="passwordForm.oldPassword"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="请输入当前密码"
            aria-label="当前密码"
          />
        </ElFormItem>
        <ElFormItem label="新密码">
          <ElInput
            v-model="passwordForm.newPassword"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="至少 6 位"
            aria-label="新密码"
          />
        </ElFormItem>
        <ElFormItem label="确认新密码">
          <ElInput
            v-model="passwordForm.confirmPassword"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="再次输入新密码"
            aria-label="确认新密码"
            @keyup.enter="onChangePassword"
          />
        </ElFormItem>
        <div class="settings-view__form-actions">
          <ElButton
            type="primary"
            size="small"
            :loading="passwordSubmitting"
            aria-label="提交修改密码"
            @click="onChangePassword"
          >
            提交修改
          </ElButton>
          <ElButton size="small" text aria-label="清空密码表单" @click="resetPasswordForm">
            清空
          </ElButton>
        </div>
      </ElForm>
      <ErrorBanner :message="passwordError" title="修改密码失败" @close="passwordError = null" />
    </section>

    <section class="settings-view__card ip-panel" aria-label="界面设置">
      <h2 class="ip-section-title">界面设置</h2>
      <div class="settings-view__row">
        <div>
          <p class="settings-view__row-title">主题</p>
          <p class="settings-view__row-desc">深色数据台为默认主题，选择会持久化到 localStorage</p>
        </div>
        <div class="settings-view__switch" role="group" aria-label="主题切换">
          <button
            class="settings-view__switch-item"
            :class="{ 'is-active': ui.theme === 'dark' }"
            type="button"
            aria-label="使用深色主题"
            :aria-pressed="ui.theme === 'dark'"
            @click="ui.setTheme('dark')"
          >
            深色
          </button>
          <button
            class="settings-view__switch-item"
            :class="{ 'is-active': ui.theme === 'light' }"
            type="button"
            aria-label="使用浅色主题"
            :aria-pressed="ui.theme === 'light'"
            @click="ui.setTheme('light')"
          >
            浅色
          </button>
        </div>
      </div>
      <div class="settings-view__row">
        <div>
          <p class="settings-view__row-title">导航栏</p>
          <p class="settings-view__row-desc">收起后仅显示图标，切换状态会被记住</p>
        </div>
        <ElButton size="small" aria-label="切换导航栏收起状态" @click="ui.toggleSidebar()">
          {{ ui.sidebarCollapsed ? '展开导航' : '收起导航' }}
        </ElButton>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.settings-view {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;

  &__input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
    pointer-events: none;
  }

  &__card {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px;
  }

  &__card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  &__card-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  &__status {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__user {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }

  &__user-info {
    flex: 1 1 220px;
    min-width: 0;
  }

  &__username {
    font-size: 15px;
    font-weight: 600;
  }

  &__meta {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__tags {
    display: flex;
    gap: 6px;
    margin-top: 4px;
    flex-wrap: wrap;
  }

  &__user-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }

  &__upload {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  &__upload-percent {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-2);
  }

  &__path {
    font-size: var(--ip-fs-xs);
    word-break: break-all;
  }

  &__classes {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  &__class-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }

  &__empty-note {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__note {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    line-height: 1.6;
  }

  &__form {
    max-width: 420px;
  }

  &__form-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    padding: 8px 0;
    border-top: 1px solid var(--ip-line-soft);
  }

  &__row-title {
    font-size: var(--ip-fs-sm);
    color: var(--ip-text);
  }

  &__row-desc {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__switch {
    display: inline-flex;
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
    overflow: hidden;
    flex: 0 0 auto;
  }

  &__switch-item {
    padding: 4px 12px;
    font-size: var(--ip-fs-xs);
    background: transparent;
    border: none;
    color: var(--ip-text-2);
    cursor: pointer;
  }

  &__switch-item.is-active {
    background: var(--ip-primary-weak);
    color: var(--ip-text);
  }
}
</style>
