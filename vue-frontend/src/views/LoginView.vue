<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton, ElForm, ElFormItem, ElInput, ElTabPane, ElTabs } from 'element-plus'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import { useUserStore } from '@/stores/user'
import { useUiStore } from '@/stores/ui'
import { notifySuccess } from '@/utils/notify'

const user = useUserStore()
const ui = useUiStore()
const route = useRoute()
const router = useRouter()

const activeTab = ref<'login' | 'register'>('login')
const submitting = ref(false)
const errorMessage = ref<string | null>(null)
const errorDetail = ref<string | null>(null)

const loginForm = reactive({ username: '', password: '' })
const registerForm = reactive({ username: '', password: '', confirm: '' })

const redirectTarget = computed(() => {
  const value = route.query.redirect
  if (typeof value === 'string' && value.length > 0 && value !== '/login') return value
  return '/detect'
})

watch(activeTab, () => {
  errorMessage.value = null
  errorDetail.value = null
})

function clearError(): void {
  errorMessage.value = null
  errorDetail.value = null
}

function validateUsername(value: string): string | null {
  const trimmed = value.trim()
  if (trimmed.length < 3) return '用户名至少 3 个字符'
  if (trimmed.length > 32) return '用户名最多 32 个字符'
  return null
}

function validatePassword(value: string): string | null {
  if (value.length < 6) return '密码至少 6 位'
  if (value.length > 64) return '密码最多 64 位'
  return null
}

async function onLogin(): Promise<void> {
  clearError()
  const usernameError = validateUsername(loginForm.username)
  if (usernameError) {
    errorMessage.value = usernameError
    return
  }
  const passwordError = validatePassword(loginForm.password)
  if (passwordError) {
    errorMessage.value = passwordError
    return
  }
  submitting.value = true
  try {
    const current = await user.login(loginForm.username.trim(), loginForm.password)
    notifySuccess(`欢迎回来，${current.username}`)
    await router.replace(redirectTarget.value)
  } catch (err) {
    const message = (err as { message?: string })?.message ?? '登录失败'
    errorMessage.value = message
    const status = (err as { status?: number | null })?.status
    if (status === 401) {
      errorDetail.value = '用户名或密码不正确；若为首次使用请先切换到「注册」创建账号'
    } else if (status === null) {
      errorDetail.value = '无法连接后端服务，请确认 uvicorn 已在 127.0.0.1:8000 运行'
    }
  } finally {
    submitting.value = false
  }
}

async function onRegister(): Promise<void> {
  clearError()
  const usernameError = validateUsername(registerForm.username)
  if (usernameError) {
    errorMessage.value = usernameError
    return
  }
  const passwordError = validatePassword(registerForm.password)
  if (passwordError) {
    errorMessage.value = passwordError
    return
  }
  if (registerForm.password !== registerForm.confirm) {
    errorMessage.value = '两次输入的密码不一致'
    return
  }
  submitting.value = true
  const username = registerForm.username.trim()
  const password = registerForm.password
  try {
    const created = await user.register(username, password)
    notifySuccess(`账号「${created.username}」创建成功，正在登录…`)
    registerForm.username = ''
    registerForm.password = ''
    registerForm.confirm = ''
    // 注册成功后直接登录，省去用户再输一次
    loginForm.username = username
    loginForm.password = password
    const current = await user.login(username, password)
    notifySuccess(`欢迎，${current.username}`)
    await router.replace(redirectTarget.value)
  } catch (err) {
    const message = (err as { message?: string })?.message ?? '注册失败'
    errorMessage.value = message
    const status = (err as { status?: number | null })?.status
    if (status === 400) errorDetail.value = '用户名可能已被占用，请更换后重试'
    else if (status === null) errorDetail.value = '无法连接后端服务，请确认 uvicorn 已在 127.0.0.1:8000 运行'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-view">
    <button
      class="login-view__theme"
      type="button"
      :aria-label="ui.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
      :title="ui.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
      @click="ui.toggleTheme()"
    >
      <!-- 与顶栏同一套日/月图标语言：深色时给"太阳"（点完变亮），浅色时给"月亮" -->
      <svg v-if="ui.theme === 'dark'" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
        <circle cx="8" cy="8" r="3.2" fill="currentColor" />
        <path
          d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3.1 3.1l1.1 1.1M11.8 11.8l1.1 1.1M12.9 3.1l-1.1 1.1M4.2 11.8l-1.1 1.1"
          stroke="currentColor"
          stroke-width="1.3"
          stroke-linecap="round"
        />
      </svg>
      <svg v-else viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
        <path
          d="M13.5 10.2A5.8 5.8 0 0 1 5.8 2.5a5.8 5.8 0 1 0 7.7 7.7Z"
          fill="currentColor"
        />
      </svg>
    </button>

    <div class="login-view__shell">
      <!-- 左侧品牌区：把原先"卡片悬在大片空黑里"的观感补上内容与信息，
           让首屏就说清"这是什么、能做什么"（窄窗口会自然换行到表单上方） -->
      <aside class="login-view__hero">
        <div class="login-view__brand">
          <svg viewBox="0 0 32 32" width="34" height="34" aria-hidden="true">
          <path
            d="M16 29c0-7 2.2-12.6 8.4-16.8M16 29c0-7-2.2-12.6-8.4-16.8M16 29v-8.4"
            fill="none"
            stroke="var(--ip-primary)"
            stroke-width="1.8"
            stroke-linecap="round"
          />
          <path
            d="M16 12.6c3.6 0 6.4-2.2 7.6-5.8-3.9-1.1-6.7 1.1-7.6 4.7M16 12.6c-3.6 0-6.4-2.2-7.6-5.8 3.9-1.1 6.7 1.1 7.6 4.7"
            fill="none"
            stroke="var(--ip-primary)"
            stroke-width="1.6"
            stroke-linecap="round"
          />
        </svg>
        <div>
          <h1 class="login-view__title">病虫害智能监测系统</h1>
          <p class="login-view__subtitle">Insect Pest Monitor</p>
        </div>
        </div>

        <h2 class="login-view__hero-title">一台树莓派，看住一片田</h2>
        <ul class="login-view__hero-points">
          <li>本机摄像头按间隔自动抓拍，<b>断网也能跑</b></li>
          <li>ONNX 推理跑在树莓派 CPU 上，不需要额外显卡</li>
          <li>历史记录 · 统计看板 · 报告导出（xlsx / docx / html）</li>
          <li>15 类害虫的危害特征与防治参考</li>
        </ul>
        <p class="login-view__hero-foot">采集 → 推理 → 看板，全部在同一台设备上完成</p>
      </aside>

      <div class="login-view__panel">
        <ElTabs v-model="activeTab" class="login-view__tabs">
        <ElTabPane label="登录" name="login">
          <ElForm label-position="top" @submit.prevent="onLogin">
            <ElFormItem label="用户名">
              <ElInput
                v-model="loginForm.username"
                placeholder="请输入用户名"
                autocomplete="username"
                clearable
                aria-label="用户名"
                @keyup.enter="onLogin"
              />
            </ElFormItem>
            <ElFormItem label="密码">
              <ElInput
                v-model="loginForm.password"
                type="password"
                placeholder="请输入密码"
                autocomplete="current-password"
                show-password
                aria-label="密码"
                @keyup.enter="onLogin"
              />
            </ElFormItem>
            <ElButton
              class="login-view__submit"
              type="primary"
              :loading="submitting"
              aria-label="登录"
              @click="onLogin"
            >
              登录
            </ElButton>
          </ElForm>
        </ElTabPane>

        <ElTabPane label="注册" name="register">
          <ElForm label-position="top" @submit.prevent="onRegister">
            <ElFormItem label="用户名">
              <ElInput
                v-model="registerForm.username"
                placeholder="3 - 32 个字符"
                autocomplete="username"
                clearable
                aria-label="注册用户名"
              />
            </ElFormItem>
            <ElFormItem label="密码">
              <ElInput
                v-model="registerForm.password"
                type="password"
                placeholder="至少 6 位"
                autocomplete="new-password"
                show-password
                aria-label="注册密码"
              />
            </ElFormItem>
            <ElFormItem label="确认密码">
              <ElInput
                v-model="registerForm.confirm"
                type="password"
                placeholder="再次输入密码"
                autocomplete="new-password"
                show-password
                aria-label="确认密码"
                @keyup.enter="onRegister"
              />
            </ElFormItem>
            <ElButton
              class="login-view__submit"
              type="primary"
              :loading="submitting"
              aria-label="注册并登录"
              @click="onRegister"
            >
              注册并登录
            </ElButton>
          </ElForm>
        </ElTabPane>
      </ElTabs>

      <ErrorBanner
        :message="errorMessage"
        :detail="errorDetail"
        title="操作未成功"
        @close="clearError"
      />

      <p class="login-view__foot">
        旧桌面端账号可直接用原用户名登录。
      </p>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.login-view {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
  /* 左上主色光晕之外，右下补一记对称的更淡光斑：让大面积底色有呼吸感，
     但两处都只用 6-8% 透明度，仍然是"安静的数据台"而不是营销页 */
  background:
    radial-gradient(46% 38% at 88% 94%, var(--ip-primary-weak) 0%, transparent 100%),
    radial-gradient(120% 80% at 12% 8%, var(--ip-primary-weak) 0%, transparent 62%),
    var(--ip-bg);

  &__theme {
    position: absolute;
    right: 20px;
    top: 18px;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    padding: 0;
    border: 1px solid var(--ip-line);
    border-radius: 50%;
    background: var(--ip-panel);
    color: var(--ip-text-2);
    cursor: pointer;
    transition:
      color var(--ip-dur-fast) var(--ip-ease),
      border-color var(--ip-dur-fast) var(--ip-ease),
      transform var(--ip-dur-fast) var(--ip-ease);

    &:hover {
      color: var(--ip-primary-hover);
      border-color: var(--ip-primary-line);
      transform: translateY(-1px);
    }

    &:active {
      transform: translateY(0);
    }

    /* 日月切换时图标转半圈，给"模式切换"一个物理隐喻 */
    svg {
      transition: transform var(--ip-dur) var(--ip-ease);
    }

    &:hover svg {
      transform: rotate(90deg);
    }
  }

  &__shell {
    display: flex;
    align-items: stretch;
    flex-wrap: wrap;
    justify-content: center;
    gap: 22px;
    width: 100%;
    max-width: 908px;
    animation: ip-login-rise 0.42s var(--ip-ease) both;
  }

  /* 进场：整体从下方 14px 淡入；表单卡片再晚 80ms，形成轻微的错峰层次 */
  &__hero {
    animation: ip-login-rise 0.5s var(--ip-ease) 0.05s both;
  }

  &__panel {
    animation: ip-login-rise 0.5s var(--ip-ease) 0.13s both;
  }

  @keyframes ip-login-rise {
    from {
      opacity: 0;
      transform: translateY(14px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  /* 品牌/说明区：给首屏内容，避免一张小卡片悬在大片空黑里 */
  &__hero {
    flex: 1 1 380px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 14px;
    padding: 8px 4px;
    /* 光晕已上移到整页背景（见 .login-view），这里不再重复叠加 */
    border-radius: var(--ip-radius-lg);
  }

  &__hero-title {
    font-size: 26px;
    font-weight: 650;
    line-height: 1.3;
    letter-spacing: 0.01em;
    color: var(--ip-text);
  }

  &__hero-points {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;

    li {
      position: relative;
      padding-left: 18px;
      font-size: var(--ip-fs-sm);
      line-height: 1.6;
      color: var(--ip-text-2);

      &::before {
        content: '';
        position: absolute;
        left: 2px;
        top: 0.62em;
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--ip-primary);
      }

      b {
        color: var(--ip-text);
      }
    }
  }

  &__hero-foot {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__panel {
    flex: 0 1 396px;
    width: 100%;
    max-width: 396px;
    align-self: center;
    padding: 22px 24px 18px;
    background: var(--ip-panel);
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-lg);
    box-shadow: var(--ip-shadow-md);
  }

  &__brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  &__title {
    font-size: 17px;
    font-weight: 600;
    letter-spacing: 0.01em;
  }

  &__subtitle {
    margin-top: 2px;
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    line-height: 1.5;
  }

  &__tabs {
    margin-bottom: 4px;
  }

  &__submit {
    width: 100%;
    margin-top: 4px;
  }

  &__foot {
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid var(--ip-line-soft);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    line-height: 1.6;
  }
}
</style>

