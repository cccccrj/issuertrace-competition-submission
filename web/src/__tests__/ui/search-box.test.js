/** 侧栏顶部的检索框：`/` 唤起、防抖、回车进结果页。
 *
 *  防抖不是顺手加的优化——站里十个搜索框此前每敲一个字发一次请求，而这一个
 *  一次要问七张表、其中一路走 FTS。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const calls = []
const pushed = []

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      search: vi.fn(async (params) => {
        calls.push(params)
        return { query: params.q, limit: params.limit, groups: [
          { kind: 'event', label: '事件', total: 2, more: { path: '/events', query: { search: params.q } },
            items: [{ event_id: 'e1', company_id: 'cxmt', summary: '长鑫募资 295 亿元',
                      event_time: '2026-05-27', risk_level: 'medium' }] },
        ] }
      }),
    },
  }
})

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useRouter: () => ({ push: (to) => pushed.push(to) }),
    useRoute: () => ({ query: {}, params: {} }),
  }
})

import SearchBox from '../../components/SearchBox.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a><slot /></a>' } }

function render() {
  return mount(SearchBox, { global: { stubs: STUBS }, attachTo: document.body })
}

const press = (key, target = document.body) => {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  Object.defineProperty(event, 'target', { value: target })
  document.dispatchEvent(event)
  return event
}

beforeEach(() => {
  calls.length = 0
  pushed.length = 0
  vi.useRealTimers()
})

describe('全局检索框', () => {
  it('在页面任意处按 / 就能开始打字', async () => {
    const wrapper = render()
    const input = wrapper.find('input').element
    expect(document.activeElement).not.toBe(input)
    press('/')
    expect(document.activeElement).toBe(input)
    wrapper.unmount()
  })

  it('焦点已经在输入框里时，/ 是一个字符不是快捷键', async () => {
    // 复核台的 1/2/3 用的是同一个守卫（web/src/hotkey.js），这里不该有第二套判断。
    const wrapper = render()
    const input = wrapper.find('input').element
    const event = press('/', input)
    expect(event.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  it('连着敲三个字只发一次请求', async () => {
    vi.useFakeTimers()
    const wrapper = render()
    const input = wrapper.find('input')
    for (const value of ['募', '募资', '募资2']) {
      await input.setValue(value)
      vi.advanceTimersByTime(80)
    }
    expect(calls.length).toBe(0)
    vi.advanceTimersByTime(300)
    await Promise.resolve()
    await wrapper.vm.$nextTick()
    expect(calls.map((call) => call.q)).toEqual(['募资2'])
    // 下拉每组只给三条，看全部走结果页。
    expect(calls[0].limit).toBe(3)
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('回车进结果页，把词带在地址上', async () => {
    const wrapper = render()
    await wrapper.find('input').setValue('生物安全法案')
    await wrapper.find('input').trigger('keydown.enter')
    expect(pushed).toEqual([{ path: '/search', query: { q: '生物安全法案' } }])
    wrapper.unmount()
  })

  it('空词不发请求也不跳转', async () => {
    const wrapper = render()
    await wrapper.find('input').setValue('   ')
    await wrapper.find('input').trigger('keydown.enter')
    expect(pushed).toEqual([])
    wrapper.unmount()
  })
})
