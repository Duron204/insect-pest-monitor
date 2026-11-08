/**
 * 换账号时清掉 SPA 里的残留状态。
 *
 * 数据本身已经由后端按账号过滤（见 backend/tests/test_account_isolation.py），
 * 但 store 里的**上一次结果**还留在内存里：A 退出、B 登录的同一个浏览器上，
 * B 会先看到 A 的历史列表与未读告警数，直到下一次请求回来才被替换。
 * 那几百毫秒里显示的是别人数据 —— 这不是缓存问题，是串号。
 *
 * 只测"清得干净"，不测样式：这些是纯状态操作。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  logout: vi.fn(),
  me: vi.fn(),
  register: vi.fn(),
  changePassword: vi.fn(),
  uploadAvatar: vi.fn(),
  normalizeMediaUrl: (path: string | null) => path
}))

vi.mock('@/utils/notify', () => ({
  notifyApiError: vi.fn(() => '出错了'),
  notifySuccess: vi.fn(),
  notifyError: vi.fn(),
  notifyInfo: vi.fn(),
  notifyWarning: vi.fn()
}))

import * as authApi from '@/api/auth'
import { useAlertsStore } from '@/stores/alerts'
import { useDevicesStore } from '@/stores/devices'
import { useHistoryStore } from '@/stores/history'
import { useUserStore } from '@/stores/user'
import type { TaskOut, UserOut } from '@/types'

const loginMock = vi.mocked(authApi.login)

function task(id: number): TaskOut {
  return {
    id,
    mode: 'image',
    status: 'done',
    source_name: 'a.jpg',
    model_id: null,
    plot_id: null,
    device_id: null,
    conf: 0.25,
    iou: 0.5,
    started_at: '2026-10-01T00:00:00',
    finished_at: '2026-10-01T00:00:01',
    frame_count: 1,
    total_frames: 1,
    duration_ms: 1000,
    total_objects: 2,
    unique_objects: 2,
    result_image_path: '/media/outputs/000001/annotated.jpg?mt=x',
    result_video_path: null,
    error: null
  }
}

function user(username: string): UserOut {
  return {
    id: 1,
    username,
    avatar_path: null,
    must_reset_password: false,
    created_at: '2026-10-01T00:00:00',
    last_login_at: null
  }
}

describe('换账号时清空残留状态', () => {
  let history: ReturnType<typeof useHistoryStore>
  let alerts: ReturnType<typeof useAlertsStore>
  let devices: ReturnType<typeof useDevicesStore>
  let userStore: ReturnType<typeof useUserStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    history = useHistoryStore()
    alerts = useAlertsStore()
    devices = useDevicesStore()
    userStore = useUserStore()
    vi.clearAllMocks()

    // 造一份"上一个账号留下的界面"
    history.items = [task(1), task(2)]
    history.total = 2
    history.page = 3
    history.filters.mode = 'video'
    alerts.unacked = 7
    devices.upsert({
      id: 1,
      hardware_id: 'pi-1',
      name: '盒子',
      model: null,
      status: 'active',
      plot_id: null,
      agent_version: null,
      backend: null,
      last_seen_at: null,
      last_frame_at: null,
      last_task_at: null,
      created_at: '2026-10-01T00:00:00',
      bound_at: null,
      online: true,
      stream_fps: 2,
      // 盒子自报的诊断字段：默认全是 null（= 盒子没上报过）
      agent_streaming: null,
      agent_stream_enabled: null,
      agent_live_detection: null,
      agent_frames_sent: null,
      agent_uploads: null,
      agent_pending_uploads: null,
      agent_last_error: null,
      agent_last_error_at: null,
      agent_status_at: null,
      agent_camera_backend: null
    })
  })

  it('history.reset 清列表、总数、分页与筛选', () => {
    history.reset()
    expect(history.items).toEqual([])
    expect(history.total).toBe(0)
    expect(history.page).toBe(1)
    expect(history.filters).toEqual({ mode: '', status: '', dateRange: null })
  })

  it('alerts.reset 清掉红点', () => {
    alerts.reset()
    expect(alerts.unacked).toBe(0)
  })

  it('clearSession 把设备、历史、告警一起清掉', () => {
    userStore.clearSession()

    expect(history.items).toEqual([])
    expect(alerts.unacked).toBe(0)
    expect(devices.items).toEqual([])
    expect(userStore.isAuthenticated).toBe(false)
  })

  it('登录成功同样清一遍 —— 未退出就换账号时也不能留着上一个人的列表', async () => {
    loginMock.mockResolvedValue({
      access_token: 'b-token',
      token_type: 'bearer',
      expires_in: 3600,
      user: user('neighbour')
    })

    await userStore.login('neighbour', 'neighbour123456')

    expect(history.items).toEqual([])
    expect(alerts.unacked).toBe(0)
    expect(devices.items).toEqual([])
    expect(userStore.user?.username).toBe('neighbour')
  })
})
