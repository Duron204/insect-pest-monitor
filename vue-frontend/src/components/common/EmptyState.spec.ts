import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import EmptyState from './EmptyState.vue'

/**
 * 空状态是"唯一的下一步"的落点：按钮必须能派发 action，
 * 没有 actionText 时不该渲染一个点了没反应的按钮。
 */

describe('EmptyState', () => {
  it('渲染说明与提示', () => {
    const wrapper = mount(EmptyState, {
      props: { description: '还没有检测记录', hint: '先上传一张图片试试' }
    })
    expect(wrapper.text()).toContain('还没有检测记录')
    expect(wrapper.text()).toContain('先上传一张图片试试')
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('点击行动按钮派发 action 事件', async () => {
    const wrapper = mount(EmptyState, {
      props: { description: '尚未选择检测图片', actionText: '选择图像' }
    })
    const button = wrapper.get('button')
    expect(button.text()).toBe('选择图像')
    await button.trigger('click')
    expect(wrapper.emitted('action')).toHaveLength(1)
  })

  it('compact 模式加上修饰类', () => {
    const wrapper = mount(EmptyState, { props: { description: '空', compact: true } })
    expect(wrapper.classes()).toContain('empty-state--compact')
  })
})
