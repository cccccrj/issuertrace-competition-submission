/** 研判助手的流式读取：事件可能被网络拆在两个分块中间，解析不能丢、不能重。 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api.js'

function respond(chunks, init = { status: 200 }) {
  const encoder = new TextEncoder()
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { ...init, headers: { 'Content-Type': 'text/event-stream' } })
}

afterEach(() => { vi.unstubAllGlobals() })

describe('askAssistantStream', () => {
  it('跨分块的事件照样解析，步骤按到达顺序回调，返回最后的结果', async () => {
    const text = 'data: {"type":"step","step":{"label":"查看风险事项"}}\n\n'
      + 'data: {"type":"step","step":{"label":"检索披露原文"}}\n\n'
      + 'data: {"type":"result","result":{"available":true,"answer":[]}}\n\n'
    // 故意切在事件中间、甚至「\n\n」中间。
    const chunks = [text.slice(0, 17), text.slice(17, 61), text.slice(61, 62), text.slice(62)]
    vi.stubGlobal('fetch', vi.fn(async () => respond(chunks)))
    const seen = []
    const result = await api.askAssistantStream('risk', 'a1', '问题', (step) => seen.push(step.label))
    expect(seen).toEqual(['查看风险事项', '检索披露原文'])
    expect(result).toEqual({ available: true, answer: [] })
  })

  it('流里没有结果就报「中断」，而不是返回空', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(['data: {"type":"step","step":{}}\n\n'])))
    await expect(api.askAssistantStream('risk', 'a1', '问题', () => {})).rejects.toThrow('回答中断了')
  })

  it('开流之前的 404 照常报成人话', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: '要找的内容不存在或已被移除' }),
                                                          { status: 404 })))
    await expect(api.askAssistantStream('risk', 'nope', '问题', () => {})).rejects.toThrow('要找的内容不存在')
  })
})
