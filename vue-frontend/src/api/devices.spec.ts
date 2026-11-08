import { describe, expect, it } from 'vitest'

import { streamUrl } from './devices'

/**
 * `streamUrl` 是唯一需要在这里测的接口函数：它是**纯函数**，而且拼错了不会
 * 报错 —— `<img src>` 只会安静地不显示画面。
 *
 * 三件事必须有牙：
 *   1. 参数名就叫 `ticket`（后端 `Query(min_length=1)` 按名字取）；
 *   2. 票据必须被正确**百分号编码**（JWT 里可能有 `+` `/` `=`，
 *      不编码的话 `+` 会被当成空格，后端解出来的票据是坏的）；
 *   3. 同一张票据配不同的 `cacheBust` 必须得到**不同的地址** ——
 *      这是"换票后画面能重新拉起来"的全部机制：`<img>` 在出错后会自动重试
 *      同一个 URL，地址不变它就永远拿到那份失败响应/过期票据。
 */
function paramsOf(url: string): URLSearchParams {
  // 用 URL 解析而不是字符串匹配：`ticket=t` 里恰好含有 "t=" 子串，
  // 用 `not.toContain('t=')` 这种写法会假通过。
  return new URL(url, 'http://localhost').searchParams
}

describe('streamUrl', () => {
  it('带上 /api 前缀，票据走 ticket 参数', () => {
    const url = streamUrl(7, 'plain-ticket')
    expect(url.startsWith('/api/devices/7/stream?')).toBe(true)
    expect(paramsOf(url).get('ticket')).toBe('plain-ticket')
  })

  it('票据里的特殊字符必须被编码，且解码后与原文完全一致', () => {
    const ticket = 'a+b/c=d.e'
    const encoded = paramsOf(streamUrl(7, ticket)).get('ticket')
    // URLSearchParams.get 已经解过码：能原样取回就说明编码是对的
    expect(encoded).toBe(ticket)
    // 直接看地址串，确认确实编码了（`+` 不编码会被后端当成空格）
    expect(streamUrl(7, ticket)).toContain('a%2Bb%2Fc%3Dd.e')
  })

  it('换一个 cacheBust 必须得到不同的地址', () => {
    expect(streamUrl(7, 'same', 1)).not.toBe(streamUrl(7, 'same', 2))
    expect(paramsOf(streamUrl(7, 'same', 1)).get('t')).toBe('1')
  })

  it('不传 cacheBust 时不带 t 参数', () => {
    expect(paramsOf(streamUrl(7, 'x')).has('t')).toBe(false)
  })

  it('不同设备拼出不同地址（票据是绑设备的）', () => {
    expect(streamUrl(1, 'x')).not.toBe(streamUrl(2, 'x'))
  })
})
