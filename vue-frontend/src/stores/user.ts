import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  changePassword as changePasswordApi,
  login as loginApi,
  logout as logoutApi,
  me as meApi,
  normalizeMediaUrl,
  register as registerApi,
  uploadAvatar as uploadAvatarApi
} from '@/api/auth'
import { clearToken, getToken, setToken, USER_KEY } from '@/api/client'
import { readJson, writeJson } from '@/utils/storage'
import { useAlertsStore } from './alerts'
import { useDevicesStore } from './devices'
import { useHistoryStore } from './history'
import type { UserOut } from '@/types'

export const useUserStore = defineStore('user', () => {
  const token = ref<string | null>(getToken())
  const user = ref<UserOut | null>(readJson<UserOut | null>(USER_KEY, null))
  const loading = ref(false)
  const error = ref<string | null>(null)
  const mustResetPassword = ref(false)

  const isAuthenticated = computed(() => token.value !== null && token.value.length > 0)
  const displayName = computed(() => user.value?.username ?? '未登录')
  const avatarUrl = computed(() => normalizeMediaUrl(user.value?.avatar_path))
  const initial = computed(() => {
    const name = user.value?.username ?? ''
    return name.length > 0 ? name.slice(0, 1).toUpperCase() : '?'
  })

  function persistUser(value: UserOut | null): void {
    user.value = value
    if (value) writeJson(USER_KEY, value)
  }

  /**
   * 清掉与"当前账号"绑定的界面状态。模型清单是服务器资源，保持共享不清。
   *
   * 不做这一步的症状很具体：A 退出、B 登录的**同一个浏览器**上，B 会先看到
   * A 的历史列表与未读告警数（数据本身已经由后端按账号过滤，但 SPA 里还留着
   * 上一次的结果），点了才发现"这不是我的"。
   */
  function resetAccountState(): void {
    useDevicesStore().reset()
    useHistoryStore().reset()
    useAlertsStore().reset()
  }

  function clearSession(): void {
    token.value = null
    persistUser(null)
    mustResetPassword.value = false
    clearToken()
    resetAccountState()
  }

  async function login(username: string, password: string): Promise<UserOut> {
    loading.value = true
    error.value = null
    try {
      const response = await loginApi({ username, password })
      token.value = response.access_token
      setToken(response.access_token)
      persistUser(response.user)
      mustResetPassword.value = response.user?.must_reset_password ?? false
      // 登录成功也清一遍：正常情况下先 logout 已经清过了，但"没退出就直接
      // 换了账号"（令牌过期后重新登录）同样不能把上一个人的列表留在屏幕上。
      resetAccountState()
      return response.user
    } catch (err) {
      error.value = (err as { message?: string })?.message ?? '登录失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  async function register(username: string, password: string): Promise<UserOut> {
    loading.value = true
    error.value = null
    try {
      return await registerApi({ username, password })
    } catch (err) {
      error.value = (err as { message?: string })?.message ?? '注册失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  async function fetchMe(): Promise<UserOut | null> {
    if (!isAuthenticated.value) return null
    try {
      const current = await meApi()
      persistUser(current)
      mustResetPassword.value = current.must_reset_password ?? false
      return current
    } catch (err) {
      const status = (err as { status?: number | null })?.status
      if (status === 401) clearSession()
      return null
    }
  }

  async function updatePassword(oldPassword: string, newPassword: string): Promise<string> {
    const response = await changePasswordApi(oldPassword, newPassword)
    mustResetPassword.value = false
    if (user.value) persistUser({ ...user.value, must_reset_password: false })
    return response.message
  }

  async function updateAvatar(file: File): Promise<string> {
    const response = await uploadAvatarApi(file)
    if (user.value) persistUser({ ...user.value, avatar_path: response.avatar_path })
    return response.avatar_path
  }

  async function logout(): Promise<void> {
    try {
      await logoutApi()
    } catch {
      /* 后端不可达也要清掉本地会话 */
    } finally {
      clearSession()
    }
  }

  return {
    token,
    user,
    loading,
    error,
    mustResetPassword,
    isAuthenticated,
    displayName,
    avatarUrl,
    initial,
    login,
    register,
    fetchMe,
    updatePassword,
    updateAvatar,
    logout,
    clearSession
  }
})
