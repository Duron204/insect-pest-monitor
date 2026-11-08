import { describe, expect, it } from 'vitest'

import { normalizeError, parseContentDisposition } from './client'

/**
 * 报告导出的文件名来自后端响应头，解析错了用户就会拿到一个乱码文件名
 * （或者干脆没有扩展名）。三种写法都要覆盖。
 */

describe('parseContentDisposition', () => {
  it('优先用 filename*=UTF-8\'\' 形式并解码中文', () => {
    const header = "attachment; filename=\"report.xlsx\"; filename*=UTF-8''%E8%99%AB%E6%83%85.xlsx"
    expect(parseContentDisposition(header, 'fallback.xlsx')).toBe('虫情.xlsx')
  })

  it('退回普通 filename，带引号与不带引号都认', () => {
    expect(parseContentDisposition('attachment; filename="r.xlsx"', 'f')).toBe('r.xlsx')
    expect(parseContentDisposition('attachment; filename=r.xlsx', 'f')).toBe('r.xlsx')
  })

  it('没有头信息时用兜底文件名', () => {
    expect(parseContentDisposition(null, 'fallback.xlsx')).toBe('fallback.xlsx')
    expect(parseContentDisposition(undefined, 'fallback.xlsx')).toBe('fallback.xlsx')
    expect(parseContentDisposition('attachment', 'fallback.xlsx')).toBe('fallback.xlsx')
  })

  it('百分号编码损坏时不抛异常，退回原始片段', () => {
    expect(parseContentDisposition("attachment; filename*=UTF-8''%E8%99", 'f')).toBe('%E8%99')
  })
})

/**
 * `normalizeError` 必须**幂等**。
 *
 * 响应拦截器 reject 出去的已经是归一化结果（`{status, message, unauthorized, raw}`），
 * 调用方在 catch 里再调一次是很自然的写法。一旦真的掉进 `String(error)`，
 * 状态码会变成 `null`、消息变成字面的 "[object Object]"。
 *
 * 这曾经真的坏过：设备详情页 `status === 404` 的分支永远不触发（解绑后页面
 * 既不停止轮询也不显示"设备不存在"），实时画面把故障原因显示成 "[object Object]"。
 * 谁把这个提前返回删掉，前两个用例就会红。
 */
describe('normalizeError', () => {
  it('对已经归一化的对象再调用一次，原样返回', () => {
    const normalized = {
      status: 404,
      message: '设备不存在',
      unauthorized: false,
      raw: new Error('x')
    }
    const again = normalizeError(normalized)
    expect(again).toBe(normalized)
    expect(again.status).toBe(404)
    expect(again.message).toBe('设备不存在')
  })

  it('归一化两次之后，404 仍然认得出是 404', () => {
    const normalized = { status: 404, message: '设备不存在', unauthorized: false, raw: {} }
    expect(normalizeError(normalizeError(normalized)).status).toBe(404)
    expect(normalizeError(normalizeError(normalized)).message).toBe('设备不存在')
  })

  it('普通 Error 仍然走原来的分支', () => {
    const out = normalizeError(new Error('炸了'))
    expect(out.status).toBeNull()
    expect(out.message).toBe('炸了')
    expect(out.unauthorized).toBe(false)
  })
})
