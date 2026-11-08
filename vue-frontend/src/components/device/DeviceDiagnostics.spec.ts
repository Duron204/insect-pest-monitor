import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import DeviceDiagnostics from './DeviceDiagnostics.vue'
import type { DeviceOut } from '@/types'

/**
 * 调试面板上每一个数字都来自盒子自报，中心只转存 —— 所以这块面板
 * **最危险的失败不是渲染错，而是说谎**。下面守的三条都是"看起来正常、其实骗人"：
 *
 *   1. `null` 必须显示成「未上报」，不能是 0 / false（否则会把"旧版本盒子"说成"摄像头坏了"）
 *   2. 有错误时面板必须**自己展开**（报错正是它存在的理由）
 *   3. 关掉的推流开关必须带一句结论（"在线却没画面"最常见的原因就是它）
 */
function device(overrides: Partial<DeviceOut> = {}): DeviceOut {
  return {
    id: 1,
    hardware_id: 'pi-0001',
    name: '东头盒子',
    model: 'Raspberry Pi 5 Model B',
    status: 'active',
    plot_id: null,
    agent_version: null,
    backend: 'onnx',
    last_seen_at: null,
    last_frame_at: null,
    last_task_at: null,
    created_at: '2026-10-01T00:00:00',
    bound_at: '2026-10-01T00:05:00',
    online: true,
    stream_fps: 0,
    agent_streaming: null,
    agent_stream_enabled: null,
    agent_live_detection: null,
    agent_frames_sent: null,
    agent_uploads: null,
    agent_pending_uploads: null,
    agent_last_error: null,
    agent_last_error_at: null,
    agent_status_at: null,
    agent_camera_backend: null,
    ...overrides
  }
}

describe('DeviceDiagnostics', () => {
  it('⭐ 没上报过的字段一律显示「未上报」，绝不显示 0 或 false', () => {
    const wrapper = mount(DeviceDiagnostics, { props: { device: device() } })
    const text = wrapper.text()

    expect(text).toContain('未上报')
    // 这几行最容易被人写成 0：写成 0 就成了"盒子报了 0 帧"
    expect(text).not.toContain('已推帧数 0')
    expect(text).not.toContain('已连接')
    // 一次都没自报过时，要把原因摆出来 —— 大多数情况是盒子跑的旧版本
    expect(text).toContain('还没有自报过状态')
  })

  it('⭐ 盒子报了错误就自动展开，并把错误原文与时刻一起显示', async () => {
    const wrapper = mount(DeviceDiagnostics, {
      props: {
        device: device({
          agent_last_error: '推流取帧失败：CameraError: 打不开 /dev/video0',
          agent_last_error_at: '2026-11-01T10:00:00',
          agent_status_at: '2026-11-01T10:00:05'
        })
      }
    })
    // 报错是展开这块面板唯一真正必要的时刻，不该等着人去点
    expect((wrapper.element as HTMLDetailsElement).open).toBe(true)
    expect(wrapper.text()).toContain('打不开 /dev/video0')
    expect(wrapper.text()).toContain('最近一次错误')
    // 必须说清它不会被自动清空，否则旧错误会被当成正在发生
    expect(wrapper.text()).toContain('不会被自动清空')
  })

  it('推流开关关掉时要给出结论 —— 这是「在线却没画面」最常见的原因', () => {
    const wrapper = mount(DeviceDiagnostics, {
      props: {
        device: device({
          agent_streaming: false,
          agent_stream_enabled: false,
          agent_status_at: '2026-11-01T10:00:05'
        })
      }
    })
    expect(wrapper.text()).toContain('关掉了就必然没有画面')
  })

  it('待上报有积压时提示断过网', () => {
    const wrapper = mount(DeviceDiagnostics, {
      props: {
        device: device({ agent_pending_uploads: 3, agent_status_at: '2026-11-01T10:00:05' })
      }
    })
    expect(wrapper.text()).toContain('断过网')
  })

  it('⭐ 画面来源是模拟图时必须点明"不是真实摄像头"并自动展开', () => {
    const wrapper = mount(DeviceDiagnostics, {
      props: {
        device: device({
          agent_camera_backend: 'simulated',
          agent_status_at: '2026-11-01T10:00:05'
        })
      }
    })
    // 模拟图是最容易让人误以为"实时"的情况，面板必须主动戳破
    expect((wrapper.element as HTMLDetailsElement).open).toBe(true)
    expect(wrapper.text()).toContain('模拟图（训练照片）')
    expect(wrapper.text()).toContain('不是真实摄像头')
    expect(wrapper.text()).toContain('test_media')
  })

  it('画面来源是真实摄像头时不报警、显示设备类型', () => {
    const wrapper = mount(DeviceDiagnostics, {
      props: {
        device: device({
          agent_camera_backend: 'picamera2',
          agent_status_at: '2026-11-01T10:00:05'
        })
      }
    })
    expect(wrapper.text()).toContain('CSI 摄像头（排线）')
    expect(wrapper.text()).not.toContain('不是真实摄像头')
    expect(wrapper.text()).not.toContain('test_media')
  })
})
