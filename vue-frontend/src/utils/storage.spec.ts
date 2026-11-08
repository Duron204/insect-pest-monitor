import { beforeEach, describe, expect, it } from 'vitest'

import { readJson, readString, removeKey, writeJson, writeString } from './storage'

/**
 * localStorage 封装的契约是"**永不抛异常**"：
 * 隐私模式、配额满、内容损坏都只能降级，不能让整个页面挂掉。
 */

beforeEach(() => {
  window.localStorage.clear()
})

describe('localStorage 安全封装', () => {
  it('读写往返', () => {
    writeString('k', 'v')
    expect(readString('k')).toBe('v')

    writeJson('obj', { theme: 'dark', n: 1 })
    expect(readJson('obj', {})).toEqual({ theme: 'dark', n: 1 })
  })

  it('缺省时返回 fallback', () => {
    expect(readString('missing', 'def')).toBe('def')
    expect(readString('missing')).toBe('')
    expect(readJson('missing', { a: 1 })).toEqual({ a: 1 })
  })

  it('内容损坏时返回 fallback 而不是抛异常', () => {
    window.localStorage.setItem('bad', '{不是合法 JSON')
    expect(readJson('bad', { ok: true })).toEqual({ ok: true })
  })

  it('removeKey 删掉键', () => {
    writeString('gone', 'x')
    removeKey('gone')
    expect(readString('gone', 'fallback')).toBe('fallback')
  })

  it('setItem 抛异常时写入静默失败（不能影响调用方）', () => {
    const original = window.localStorage.setItem
    window.localStorage.setItem = () => {
      throw new Error('QuotaExceededError')
    }
    try {
      expect(() => writeString('k', 'v')).not.toThrow()
      expect(() => writeJson('k', { a: 1 })).not.toThrow()
    } finally {
      window.localStorage.setItem = original
    }
  })
})
