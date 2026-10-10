/** 规则层与模型的分歧，在页面上。
 *
 *  两份读数一直并排存在 `processed_documents` 里，没有任何接口把它们放在一起读过。
 *  真实语料上它们在 43% 的文档上不一致，规则层判为事件的三分之一被模型否决——
 *  而这批分歧**一条都没有人裁定过**。
 *
 *  这里钉住的是「这个数字能说什么」：它不是准确率、不是谁的错误率，四格加起来
 *  必须正好是被比较的总数，每一格都要能翻开。把它说成错误率，或者让格子上的数
 *  和翻开后的列表对不上，都是把一个未经裁定的分歧包装成结论。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const LAYERS = {
  available: true,
  compared: 2803,
  cases: [
    { key: 'agree_event', label: '两层都判为事件', count: 1454, agrees: true },
    { key: 'rule_only', label: '规则层判事件，系统研判否决', count: 757, agrees: false },
    { key: 'system_only', label: '规则层未判，系统研判判为事件', count: 456, agrees: false },
    { key: 'agree_none', label: '两层都未判为事件', count: 136, agrees: true },
  ],
  disagreements: 1213,
  disagreement_rate: 0.4328,
  rule_events: 2211,
  overruled: 757,
  overrule_rate: 0.3424,
  judged: 0,
}

const FUNNEL = {
  available: true,
  stages: [
    { key: 'collected', label: '采集文档数', count: 4339 },
    { key: 'included', label: '有效入库数', count: 2803 },
    { key: 'eventful', label: '识别事件数', count: 1910 },
    { key: 'with_claims', label: '可核验主张数', count: 1468 },
  ],
  inclusion: [{ value: 'included', count: 2803 }],
  eventhood: [{ value: 'EVENT', count: 1910 }],
  extraction: [{ value: 'ok', count: 3986 }],
  rejection_reasons: [{ code: 'excluded_listing_page', label: '是文章索引页，不是文章', documents: 96, occurrences: 96 }],
  labels: { inclusion: {}, eventhood: {}, extraction: {} },
}

const SOURCES = { available: true, items: [
  { source_id: 'jiemian', source_tier: 'S2', documents: 142, included: 120, events: 90, event_rate: 0.63,
    first_published: '2024-03-07', last_published: '2026-08-26' },
] }

const state = { layers: LAYERS, lastDocumentParams: null }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      ingestionFunnel: vi.fn(async () => FUNNEL),
      ingestionLayers: vi.fn(async () => state.layers),
      ingestionSources: vi.fn(async () => SOURCES),
      ingestionDocuments: vi.fn(async (params) => {
        state.lastDocumentParams = params
        return { total: 2803, limit: 50, offset: 0, items: [], labels: {} }
      }),
    },
  }
})

vi.mock('../../charts/echarts.js', () => ({ default: { init: vi.fn() } }))

import IngestionView from '../../views/IngestionView.vue'

// 这两个页面现在从 URL 读回搜索词（全局检索的「查看全部」把词带过来），
// 于是它们调用了 useRoute()；不挂路由的 mount 需要把它补上。
vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, useRoute: () => ({ query: {}, params: {} }) }
})

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }

async function render() {
  const wrapper = mount(IngestionView, { global: { stubs: STUBS } })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  state.layers = LAYERS
  state.lastDocumentParams = null
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

describe('规则层与模型的分歧', () => {
  it('四格都在，且加起来等于被比较的总数', async () => {
    const wrapper = await render()
    const buttons = wrapper.findAll('button.case')
    expect(buttons).toHaveLength(4)
    const numbers = buttons.map((b) => Number(b.text().replace(/[^\d]/g, '').slice(0, 4)))
    expect(numbers).toEqual([1454, 757, 456, 136])
    expect(1454 + 757 + 456 + 136).toBe(LAYERS.compared)
  })

  it('两个分歧率各自报出，且带各自的分母', async () => {
    // 「两层不一致」与「模型否决了规则层的事件」是两件事，分母也不同；
    // 只报一个、或共用一个分母，都会把其中一件说错。
    const text = (await render()).text()
    expect(text).toContain('43.3%')     // 分歧 / 送进模型的 2,803
    expect(text).toContain('34.2%')     // 否决 / 规则层判事件的 2,211
    expect(text).toContain('2,803')
    expect(text).toContain('2,211')
  })

  /* 2026-09-06 按产品决定移除：页面不再自述口径与方法学，说明性文字整体从站内
     撤下（约 4,100 字），这些话改由技术报告与答辩材料承担。删的是断言，不是
     曾经的判断——原句与钉它的理由见 docs/界面体检记录.md §四。
     2026-09-07 补记：导语按产品决定加回来了，但形态换了——每页一句、不超过 40 字、
     只回答「这一页在回答什么问题」，由 ui/conventions.test.js 的「每页一句导语」钉住；
     方法学与「这不是什么」归 /guide 使用指南页，不再摊回每一页。上面撤下的是后者。 */

  it('点一格就把下面的文档列表筛到那一格', async () => {
    const wrapper = await render()
    await wrapper.findAll('button.case')[1].trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(state.lastDocumentParams.layer).toBe('rule_only')
  })

  it('再点一次取消筛选', async () => {
    const wrapper = await render()
    const cell = () => wrapper.findAll('button.case')[1]
    await cell().trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))
    await cell().trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(state.lastDocumentParams.layer).toBe('')
  })

  it('选中的那一格在无障碍层面也是选中的', async () => {
    // 只靠边框颜色表示选中，读屏用户拿不到这个状态。
    const wrapper = await render()
    await wrapper.findAll('button.case')[1].trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll('button.case')[1].attributes('aria-pressed')).toBe('true')
    expect(wrapper.findAll('button.case')[0].attributes('aria-pressed')).toBe('false')
  })

  it('拿不到规则层读数时整节不出现，而不是显示成完全一致', async () => {
    // 没有数据算出来的「零分歧」是所有答案里最好看的一个。
    state.layers = { available: false, reason: '本次部署没有规则层的独立读数（`rule_eventhood`）' }
    const wrapper = await render()
    expect(wrapper.findAll('button.case')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('规则层与模型的分歧')
  })
})
