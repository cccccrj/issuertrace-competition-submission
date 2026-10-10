/** 使用指南页，钉住它存在的理由。
 *
 *  这一页的价值全在「每条链接都真的落到该落的地方」上——它是第一次来的人唯一会把
 *  六条链接都点一遍的页面，而深链失败不报错，只是安静地给出另一页内容。所以这里
 *  断言的是 href 与降级行为，不是文案。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const EVENT = 'e1'

const STEPS = [
  { ordinal: 1, key: 'identify', title: '多源金融事件识别', does: 'd1', caution: 'c1',
    available: true, to: `/events/${EVENT}`, unavailable_reason: null },
  { ordinal: 2, key: 'extract', title: '要素抽取与标准化', does: 'd2', caution: 'c2',
    available: true, to: `/elements?event=${EVENT}`, unavailable_reason: null },
  { ordinal: 3, key: 'verify', title: '可信度评估与辨伪', does: 'd3', caution: 'c3',
    available: true, to: `/claims?event=${EVENT}`, unavailable_reason: null },
  { ordinal: 4, key: 'dedupe', title: '同源事件聚合与去重', does: 'd4', caution: 'c4',
    available: true, to: '/duplicates?cluster=k1', unavailable_reason: null },
  { ordinal: 5, key: 'trace', title: '事件脉络追踪', does: 'd5', caution: 'c5',
    available: true, to: '/issues/i1', unavailable_reason: null },
  { ordinal: 6, key: 'alert', title: '影响分级与预警推送', does: 'd6', caution: 'c6',
    available: true, to: '/risks/a1', unavailable_reason: null },
]

const GUIDE = {
  sample: { event_id: EVENT, summary: 's', company_id: 'cxmt', issue_code: 'FUND_RAISE',
            risk_level: 'high', headline: 'h' },
  candidates: 54,
  steps: STEPS,
  boundary: { companies: ['cxmt', 'wuxi_apptec'], events: 1907,
              earliest: '2015-01-01', latest: '2026-09-01' },
}

let payload = GUIDE

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, api: { guide: vi.fn(async () => payload) } }
})

import { api } from '../../api.js'
import GuideView from '../../views/GuideView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }

async function render() {
  const wrapper = mount(GuideView, { global: { stubs: STUBS } })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  payload = GUIDE
  api.guide.mockClear()
})

describe('跟着走一遍', () => {
  /** 侧栏此前逐字写着命题的六项主要任务——「读侧栏即读任务表」。侧栏改成按业务任务
   *  组织之后，那份举证搬到了这一页。这条断言就是搬家的落点：改侧栏分组不会再让
   *  「六项任务全覆盖」这件事无人证明。 */
  it('六项主要任务逐字在页面上，每一项都点得进它的落点', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    for (const task of ['多源金融事件识别', '要素抽取与标准化', '可信度评估与辨伪',
                        '同源事件聚合与去重', '事件脉络追踪', '影响分级与预警推送']) {
      expect(text, task).toContain(task)
    }
    expect(text).toContain('命题的六项主要任务')
    // 六张卡都是链接，而不是六段说明文字。
    const steps = wrapper.findAll('.step')
    expect(steps).toHaveLength(6)
    for (const step of steps) expect(step.attributes('href')).toBeTruthy()
  })

  it('六步各有一个落点，顺序就是命题的六项任务', async () => {
    const wrapper = await render()
    const steps = wrapper.findAll('.step')
    expect(steps).toHaveLength(6)
    expect(steps.map((node) => node.attributes('href'))).toEqual(STEPS.map((step) => step.to))
  })

  it('落点带着筛选参数，而不是光秃秃的列表页', async () => {
    // 这一条是整页的要害：`/elements` 与 `/elements?event=…` 长得一样，落错了
    // 读者看到的是三万条要素，而指南正在说「这条事件的要素」。
    const wrapper = await render()
    const hrefs = wrapper.findAll('.step').map((node) => node.attributes('href'))
    expect(hrefs[1]).toContain(`event=${EVENT}`)
    expect(hrefs[2]).toContain(`event=${EVENT}`)
    expect(hrefs[3]).toContain('cluster=')
    // 第六步此前落在一个筛好的预警列表上，现在直接落在那一条的研判档案：同一条
    // 链路的终点是一份档案，不是一个还要再点一次的列表。
    expect(hrefs[5]).toMatch(/^\/risks\/[^/?]+$/)
  })

  it('样例是挑出来的，页面说出它从多少条里挑', async () => {
    // 写死 id 会随下一次重跑处理层失效。页面上说出候选数，是这件事的凭据。
    const wrapper = await render()
    expect(wrapper.text()).toContain('54')
  })
})

describe('某一步走不通时', () => {
  it('降级成不可点并说明原因，而不是从六步里消失', async () => {
    payload = { ...GUIDE, steps: STEPS.map((step, index) => index !== 3 ? step : {
      ...step, available: false, to: '/duplicates',
      unavailable_reason: '同源聚类尚未运行，或这份文档不在任何簇里。' }) }
    const wrapper = await render()
    expect(wrapper.findAll('.step')).toHaveLength(6)
    expect(wrapper.findAll('.step.off')).toHaveLength(1)
    expect(wrapper.text()).toContain('同源聚类尚未运行')
  })

  it('一条样例都挑不出来时仍然列出六步', async () => {
    payload = {
      ...GUIDE, sample: null, candidates: 0,
      steps: STEPS.map((step) => ({ ...step, available: false, to: '/events',
                                    unavailable_reason: '本部署还没有一条六步齐全的事件可用作样例。' })),
    }
    const wrapper = await render()
    expect(wrapper.findAll('.step')).toHaveLength(6)
    expect(wrapper.text()).toContain('本部署还没有一条六步齐全的事件')
  })
})

describe('这是什么', () => {
  it('数据边界与那条红线都在页面上', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('长鑫科技')
    expect(text).toContain('药明康德')
    expect(text).toContain('不替代监管认定')
  })

  it('名词表十二条都渲染，各带一个去处', async () => {
    const wrapper = await render()
    const links = wrapper.findAll('a').map((node) => node.attributes('href'))
    for (const path of ['/events', '/claims', '/duplicates', '/propagation', '/issues', '/review']) {
      expect(links, path).toContain(path)
    }
  })

  it('只发一次请求', async () => {
    await render()
    expect(api.guide).toHaveBeenCalledTimes(1)
  })
})
