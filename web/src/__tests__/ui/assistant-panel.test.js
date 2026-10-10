/** 研判助手面板：出处看得见、过程看得见、降级说人话。 */
import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api.js', () => ({ api: { askAssistantStream: vi.fn() } }))

import { api } from '../../api.js'
import AssistantPanel from '../../components/AssistantPanel.vue'

const RouterLink = { props: ['to'], template: '<a :href="to"><slot /></a>' }

function panel(scope = 'risk') {
  return mount(AssistantPanel, { props: { scope, objectId: 'a1' }, global: { stubs: { RouterLink } } })
}

const ANSWERED = {
  available: true, question: 'q', dropped: 1, incomplete: false,
  answer: [{ text: '该消息完全依据二手报道。', sources: ['S1'] },
           { text: '招股书提到了出口管制风险。', sources: ['S6', 'S7'] }],
  sources: {
    S1: { kind: 'risk', id: 'a1', label: '长鑫科技｜出口管制', link: '/risks/a1', excerpt: null },
    S6: { kind: 'paragraph', id: '1', label: '注册稿_招股说明书', link: null, page: 146, excerpt: '技术出口管制加强' },
    S7: { kind: 'paragraph', id: '2', label: '招股意向书', link: 'https://example.test/x.pdf', page: 171, excerpt: '同上' },
  },
  steps: [{ tool: 'risk', label: '查看风险事项', query: '当前页面', by: 'system',
            reason: '先读当前页面这一条的结论与依据', outcome: '取得 5 条资料' },
          { tool: 'evidence', label: '检索披露原文', query: '出口管制', by: 'assistant',
            reason: '事项完全依据二手报道，去招股书里找相关风险提示', outcome: '新增 5 条资料' }],
}

/** 假流：先逐步回调 onStep，再给结果——与真接口的顺序一致。 */
function streams(result) {
  api.askAssistantStream.mockImplementation(async (_scope, _id, _question, onStep) => {
    for (const step of result.steps || []) onStep(step)
    return result
  })
}

beforeEach(() => { api.askAssistantStream.mockReset() })

describe('研判助手面板', () => {
  it('输入框有可访问名，示例问题按页面范围给出', () => {
    const wrapper = panel('event')
    expect(wrapper.find('input').attributes('aria-label')).toBe('向研判助手提问')
    expect(wrapper.findAll('.chip').map((node) => node.text())).toContain('公司对这件事有没有发过公告回应？')
  })

  it('点示例即提问，带上范围与对象', async () => {
    streams(ANSWERED)
    const wrapper = panel()
    await wrapper.find('.chip').trigger('click')
    expect(api.askAssistantStream).toHaveBeenCalledWith('risk', 'a1', '这条为什么是这个风险分？', expect.any(Function))
  })

  it('每句回答后面跟出处编号，编号跳到出处列表里那一行', async () => {
    streams(ANSWERED)
    const wrapper = panel()
    await wrapper.find('input').setValue('有没有权威来源？')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    const items = wrapper.findAll('.answer li')
    expect(items).toHaveLength(2)
    expect(items[1].findAll('.cite').map((node) => node.attributes('href'))).toEqual(['#src-S6', '#src-S7'])
    expect(wrapper.find('#src-S6').text()).toContain('第 146 页')
    expect(wrapper.find('#src-S6 .excerpt').text()).toBe('技术出口管制加强')
  })

  it('站内对象走站内链接，外部原文开新窗口，没有链接的只显示名字', async () => {
    streams(ANSWERED)
    const wrapper = panel()
    await wrapper.find('.chip').trigger('click')
    await flushPromises()
    expect(wrapper.find('#src-S1 a').attributes('href')).toBe('/risks/a1')
    expect(wrapper.find('#src-S7 a').attributes('target')).toBe('_blank')
    expect(wrapper.find('#src-S6 a').exists()).toBe(false)
  })

  it('被删掉的句子与查证过程都明说', async () => {
    streams(ANSWERED)
    const wrapper = panel()
    await wrapper.find('.chip').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('另有 1 句因为指不出平台里的出处，已被删去')
    expect(wrapper.find('.steps summary').text()).toBe('查证过程（2 步）')
    expect(wrapper.find('.steps').text()).toContain('打开页面时自动查')
  })

  it('查的时候逐步显示：每一步的意图与结果，最后一行是「正在想下一步」', async () => {
    let release
    api.askAssistantStream.mockImplementation((_scope, _id, _question, onStep) => {
      onStep(ANSWERED.steps[0])
      onStep(ANSWERED.steps[1])
      return new Promise((resolve) => { release = () => resolve(ANSWERED) })
    })
    const wrapper = panel()
    await wrapper.find('.chip').trigger('click')
    await flushPromises()
    const steps = wrapper.find('.steps')
    expect(steps.attributes('open')).toBeDefined()
    expect(steps.find('summary').text()).toBe('查证过程（2 步，进行中）')
    expect(steps.text()).toContain('事项完全依据二手报道，去招股书里找相关风险提示')
    expect(steps.text()).toContain('→ 新增 5 条资料')
    expect(steps.find('.thinking').text()).toBe('正在想下一步…')
    expect(wrapper.find('.answer').exists()).toBe(false)
    release()
    await flushPromises()
    expect(wrapper.find('.thinking').exists()).toBe(false)
    expect(wrapper.find('.answer').exists()).toBe(true)
    // 答完以后过程仍然展开着，而不是自动收起。
    expect(wrapper.find('.steps').attributes('open')).toBeDefined()
  })

  it('服务不可用时显示原因，不显示空的回答区', async () => {
    streams({ available: false, reason: '智能研判服务当前不可用', steps: [] })
    const wrapper = panel()
    await wrapper.find('.chip').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('智能研判服务当前不可用')
    expect(wrapper.find('.answer').exists()).toBe(false)
  })

  it('查证没做完时提示', async () => {
    streams({ ...ANSWERED, incomplete: true })
    const wrapper = panel()
    await wrapper.find('.chip').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('查证步数已用完')
  })

  it('请求失败时就近报错，问题留在输入框里', async () => {
    api.askAssistantStream.mockRejectedValue(new Error('操作过于频繁，请稍后再试'))
    const wrapper = panel()
    await wrapper.find('input').setValue('我的问题')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('操作过于频繁')
    expect(wrapper.find('input').element.value).toBe('我的问题')
  })
})
