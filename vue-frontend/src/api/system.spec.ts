import { beforeEach, describe, expect, it, vi } from 'vitest'

// 只关心"请求长什么样"，把网络层换成记录调用的假函数。
// `vi.hoisted` 是必须的：`vi.mock` 工厂会被提升到 import 之前。
const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('./client', () => ({ get, post }))

import { agentStatus, setLiveDetection, hostStatus } from './system'

describe('system api', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
  })

  it('agentStatus 走 GET /system/agent', () => {
    agentStatus()
    expect(get).toHaveBeenCalledWith('/system/agent')
  })

  it('hostStatus 仍走 GET /system/host（两条本机状态不要混用）', () => {
    // 一个是"盒子挂没挂上中心、有没有推流"，一个是温度/磁盘。
    // 路径写错不会报类型错，只会在页面上显示另一组数据。
    hostStatus()
    expect(get).toHaveBeenCalledWith('/system/host')
  })

  it('setLiveDetection(true) 发 POST，body 是 { enabled: true }', () => {
    setLiveDetection(true)
    expect(post).toHaveBeenCalledWith('/system/agent/live-detection', { enabled: true })
  })

  it('setLiveDetection(false) 同样显式传 false —— 不能靠"缺省即关闭"', () => {
    // 后端 AgentLiveDetectionIn.enabled 是必填 bool；漏掉字段会直接 422。
    setLiveDetection(false)
    expect(post).toHaveBeenCalledWith('/system/agent/live-detection', { enabled: false })
  })
})
