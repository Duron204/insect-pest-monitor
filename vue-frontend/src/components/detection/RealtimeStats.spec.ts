/**
 * 「实时统计」面板的数字口径。
 *
 * 起因：批量模式下「去重虫数」与「累计框数」显示的是**同一个数**，下面还写着
 * "按轨迹去重后的独立目标" —— 而批量根本没有跨帧跟踪。后端 `routers/detection.py`
 * 里明写 `task.unique_objects = total_objects  # 每张图独立，不做跨图跟踪`，
 * 所以那不是 bug 而是**后端既定的口径**，错的是界面照搬了摄像头模式的文案。
 *
 * 只有摄像头与视频有轨迹（`services/tracker.py`），那里"去重 < 框数"才成立。
 * 下面把这些边界钉住：没有跟踪就**不许出现"去重"字样**。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import RealtimeStats from './RealtimeStats.vue'

function mountStats(props: Record<string, unknown> = {}) {
  return mount(RealtimeStats, {
    props: {
      frameCounts: { 合头菊蚜: 1 },
      totalObjects: 57,
      uniqueObjects: 57,
      inferenceMs: 263,
      frameTotal: 1,
      ...props
    }
  })
}

describe('RealtimeStats 的数字口径', () => {
  it('批量模式不摆"去重虫数"—— 批量下它与框数恒等', () => {
    const text = mountStats({ mode: 'batch' }).text()
    expect(text).not.toContain('去重虫数')
    expect(text).not.toContain('按轨迹去重')
    expect(text).toContain('本批框数')
    expect(text).toContain('本张框数')
  })

  it('批量模式两块磁贴的数字必须不同：整批 vs 当前这一张', () => {
    const wrapper = mountStats({ mode: 'batch', totalObjects: 57, frameTotal: 1 })
    const values = wrapper.findAll('.stats__hero-value').map((node) => node.text())
    expect(values).toEqual(['57', '1'])
  })

  it('单图模式同样不出现"去重"，只讲这张图的框数', () => {
    const text = mountStats({ mode: 'image' }).text()
    expect(text).not.toContain('去重虫数')
    expect(text).not.toContain('按轨迹去重')
    expect(text).toContain('本图框数')
    expect(mountStats({ mode: 'image' }).findAll('.stats__hero-value')).toHaveLength(1)
  })

  it('摄像头模式不重复 CameraPanel 的四项（同屏只该有一份）', () => {
    const wrapper = mountStats({
      mode: 'camera',
      uniqueObjects: 12,
      totalObjects: 57,
      activeTracks: 3,
      elapsedSeconds: 90,
      live: true
    })
    const text = wrapper.text()
    // 去重虫数 / 累计框数 / 活动轨迹 / 运行时长 归 CameraPanel 的磁贴管
    expect(text).not.toContain('去重虫数')
    expect(text).not.toContain('按轨迹去重')
    expect(text).not.toContain('活动轨迹')
    expect(text).not.toContain('累计运行')
    expect(wrapper.findAll('.stats__hero-value')).toHaveLength(0)
    // 但本帧相关的数只有这里有，不能跟着一起砍掉
    expect(text).toContain('本帧目标')
    expect(text).toContain('已处理帧')
  })

  it('视频模式与摄像头同等对待（都有轨迹）', () => {
    const text = mountStats({ mode: 'video', uniqueObjects: 3, totalObjects: 9 }).text()
    expect(text).toContain('去重虫数')
    expect(text).toContain('活动轨迹')
  })

  it('单图/批量不显示"已处理帧"与"活动轨迹"这两个没意义的数', () => {
    for (const mode of ['image', 'batch'] as const) {
      const text = mountStats({ mode }).text()
      expect(text).not.toContain('已处理帧')
      expect(text).not.toContain('活动轨迹')
    }
  })

  it('推理耗时在任何模式下都在', () => {
    for (const mode of ['image', 'batch', 'camera', 'video'] as const) {
      expect(mountStats({ mode }).text()).toContain('263 ms')
    }
  })
})
