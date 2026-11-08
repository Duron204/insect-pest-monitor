import { describe, expect, it } from 'vitest'
import { parseBlocks, parseInline } from './aiMarkdown'

describe('parseInline', () => {
  it('解析加粗与引用', () => {
    const segments = parseInline('**结论**：白粉虱高发 [1][2]')
    expect(segments).toEqual([
      { kind: 'bold', text: '结论' },
      { kind: 'text', text: '：白粉虱高发 ' },
      { kind: 'cite', n: 1 },
      { kind: 'cite', n: 2 }
    ])
  })

  it('普通文本原样返回', () => {
    expect(parseInline('没有标记的文本')).toEqual([{ kind: 'text', text: '没有标记的文本' }])
  })

  it('空字符串返回空文本段', () => {
    expect(parseInline('')).toEqual([{ kind: 'text', text: '' }])
  })

  it('孤立的星号不当加粗处理', () => {
    expect(parseInline('2 * 3 = 6')[0]!).toEqual({ kind: 'text', text: '2 * 3 = 6' })
  })
})

describe('parseBlocks', () => {
  it('段落与列表分块', () => {
    const blocks = parseBlocks('结论如下：\n\n- 第一条 [1]\n- 第二条\n\n补充说明')
    expect(blocks.map((b) => b.type)).toEqual(['paragraph', 'list', 'paragraph'])
    expect(blocks[1]!.lines).toHaveLength(2)
    expect(blocks[1]!.lines[0]![1]).toEqual({ kind: 'cite', n: 1 })
  })

  it('支持数字与中文序号列表', () => {
    const blocks = parseBlocks('1. 甲\n2. 乙\n3、丙')
    expect(blocks).toHaveLength(1)
    expect(blocks[0]!.type).toBe('list')
    expect(blocks[0]!.lines).toHaveLength(3)
  })

  it('连续行合并为一个段落', () => {
    const blocks = parseBlocks('第一行\n第二行')
    expect(blocks).toHaveLength(1)
    expect(blocks[0]!.lines).toHaveLength(2)
  })

  it('空内容返回空数组', () => {
    expect(parseBlocks('')).toEqual([])
    expect(parseBlocks('   \n  ')).toEqual([])
  })
})

