/** The landing page, checked for the three things it was rebuilt to fix.
 *
 *  It used to be eight counters, a paragraph, and company cards nobody could
 *  click — so a reader could see that the platform holds data but not what it
 *  does with it, and could not get from any number to the thing behind it. It
 *  also made one request per company on top of the first.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const calls = []

const OVERVIEW = {
  events: 1995, claims: 3717, verdicts: 3717, issues: 41, diffs: 1212,
  documents: 4339, alerts: 371, high_alerts: 136, propagation_chains: 1476,
  round: { ordinal: 10, reference_time: '2026-09-03', new_event_count: 7, is_baseline: false },
  review: { open: 297, decided: 12 },
  companies: [],
}

const PIPELINE = {
  documents: 4339,
  tasks: [
    { ordinal: 1, key: 'identify', title: '多源金融事件识别', link: '/ingestion',
      headline: { value: 1995, label: '事件' }, note: 'n1',
      breakdown: [{ value: 'included', count: 2899 }, { value: 'excluded_no_body', count: 275 }],
      rate: { value: 0.646, numerator: 2803, denominator: 4339, label: '入库率', basis: '有效入库 / 采集文档' } },
    { ordinal: 2, key: 'extract', title: '要素抽取与标准化', link: '/elements',
      headline: { value: 31383, label: '要素' }, note: 'n2',
      breakdown: [{ value: 'normalized', count: 27584 }, { value: 'unresolved', count: 3799 }] },
    { ordinal: 3, key: 'verify', title: '可信度评估与辨伪', link: '/claims',
      headline: { value: 3717, label: '原子主张' }, note: 'n3',
      breakdown: [{ value: 'SUPPORTED', count: 1762 }, { value: 'UNVERIFIABLE', count: 1238 }],
      rate: { value: null, numerator: 0, denominator: 0, label: '可核验率', basis: '非「无法核验」裁决 / 全部裁决' },
      unverifiable: { available: true, subtypes: [], groups: [
        { code: 'boundary', label: '设计边界', count: 826 }, { code: 'improvable', label: '可改进', count: 440 }] } },
    { ordinal: 4, key: 'dedupe', title: '同源事件聚合与去重', link: '/duplicates',
      headline: { value: 297, label: '同源簇' }, note: 'n4',
      breakdown: [{ value: '2', count: 188 }, { value: '209', count: 1 }] },
    { ordinal: 5, key: 'trace', title: '事件脉络追踪', link: '/timeline',
      headline: { value: 41, label: '议题' }, note: 'n5',
      breakdown: [{ value: '2026-08', count: 12 }] },
    { ordinal: 6, key: 'alert', title: '影响分级与预警推送', link: '/alerts',
      headline: { value: 371, label: '未处置预警' }, note: 'n6',
      breakdown: [{ value: 'high', count: 136 }, { value: 'medium', count: 196 }] },
  ],
  companies: [
    { company_id: 'cxmt', events: 604, needs_review: 3, evidence_paragraphs: 27129, high_alerts: 25 },
    { company_id: 'wuxi_apptec', events: 1391, needs_review: 9, evidence_paragraphs: 0, high_alerts: 111 },
  ],
  tiers: [{ value: 'S0', count: 854 }, { value: 'S1', count: 1049 }],
}

const TREND = {
  months: ['2026-08', '2026-09'],
  series: [{ key: 'high', points: [3, 1] }, { key: 'medium', points: [10, 4] }],
  phases: [],
  out_of_window: { before: 472, after: 9, earliest: '2015-01-01', latest: '2032-01-01' },
  excluded_market_data: 321,
}

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      overview: vi.fn(async () => { calls.push('overview'); return OVERVIEW }),
      overviewPipeline: vi.fn(async () => { calls.push('pipeline'); return PIPELINE }),
      overviewTrend: vi.fn(async () => { calls.push('trend'); return TREND }),
      issues: vi.fn(async () => { calls.push('issues'); return { items: [
        { issue_id: 'i1', issue_code: 'TECH_LEVEL', event_count: 120 },
        { issue_id: 'i2', issue_code: 'FUND_RAISE', event_count: 40 },
      ] } }),
    },
  }
})

// The chart library never boots in tests; the option builders are covered on
// their own, and what matters here is which panels exist and where they lead.
vi.mock('../../charts/echarts.js', () => ({ default: { init: vi.fn() } }))

import OverviewView from '../../views/OverviewView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }

async function render() {
  const wrapper = mount(OverviewView, { global: { stubs: STUBS } })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  calls.length = 0
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

describe('六项任务', () => {
  it('六个任务各一张卡，按命题的编号与顺序', async () => {
    const wrapper = await render()
    const titles = wrapper.findAll('.task .title').map((node) => node.text())
    expect(titles).toEqual([
      '多源金融事件识别', '要素抽取与标准化', '可信度评估与辨伪',
      '同源事件聚合与去重', '事件脉络追踪', '影响分级与预警推送',
    ])
  })

  it('每张卡都能点进去——首页不是死路', async () => {
    const wrapper = await render()
    const links = wrapper.findAll('.task-head').map((node) => node.attributes('href'))
    expect(links).toEqual(['/ingestion', '/elements', '/claims', '/duplicates', '/timeline', '/alerts'])
  })

  it('头条数字直接来自接口，页面不自己算', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('31,383')
    expect(text).toContain('297')
  })
})

/** 2026-09-23：卡片做成一条流水线，每张卡多一个质量率；顶部两张数字卡的两处错一并修掉。 */
describe('流水线与质量率', () => {
  it('流水线从采集文档开始，后面是六张卡的头条数字，顺序与卡片一致', async () => {
    const nodes = (await render()).findAll('.strip .node')
    expect(nodes.map((node) => node.find('.label').text()))
      .toEqual(['采集文档', '事件', '要素', '原子主张', '同源簇', '议题', '未处置预警'])
    expect(nodes[0].find('.value').text()).toBe('4,339')
  })

  it('点流水线上的节点，滚到对应那张卡并让它亮一下', async () => {
    const wrapper = await render()
    const card = wrapper.find('#task-dedupe')
    card.element.scrollIntoView = vi.fn()
    await wrapper.findAll('.strip .node')[4].trigger('click')
    expect(card.element.scrollIntoView).toHaveBeenCalled()
  })

  it('质量率照接口给的画，分母为 0 时说「暂无数据」而不是画一根 0% 的条', async () => {
    const meters = (await render()).findAll('.meter')
    expect(meters).toHaveLength(2)
    expect(meters[0].text()).toContain('入库率')
    expect(meters[0].text()).toContain('64.6%')
    expect(meters[0].text()).toContain('有效入库 / 采集文档')
    expect(meters[1].text()).toContain('暂无数据')
  })

  it('头条数字从 0 滚到接口给的终值，并且不用等宽数字', async () => {
    const wrapper = await render()
    const headline = wrapper.find('.task .headline b')
    expect(headline.classes()).not.toContain('mono')
    await new Promise((resolve) => setTimeout(resolve, 750))
    expect(headline.text()).toBe('1,995')
  })

  it('读者要求减少动效时，头条数字一帧都不滚，直接是终值', async () => {
    const original = window.matchMedia
    window.matchMedia = (query) => ({ ...original(query), matches: query.includes('reduce') })
    try {
      const headline = (await render()).find('.task .headline b')
      expect(headline.text()).toBe('1,995')
    } finally {
      window.matchMedia = original
    }
  })

  it('入库结果的图例不印英文码：表里有的每一类都有中文名', () => {
    const read = (name) => readFileSync(
      join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'views', name), 'utf8')
    const keys = (name) => [...read(name).match(/const INCLUSION_LABELS = \{([\s\S]*?)\}/)[1]
      .matchAll(/(\w+):/g)].map((m) => m[1]).sort()
    expect(keys('OverviewView.vue')).toEqual(keys('IngestionView.vue'))
  })

  it('「未处置预警」不再链到已退役的 /alerts，「待复核」不再是写死的 300', async () => {
    const wrapper = await render()
    const hrefs = wrapper.findAll('.tiles a').map((node) => node.attributes('href'))
    expect(hrefs).toContain('/risks?level=high&view=open')
    expect(hrefs.some((href) => href.startsWith('/alerts'))).toBe(false)
    const review = wrapper.findAll('.tiles a').find((node) => node.attributes('href') === '/review')
    expect(review.text()).toContain('297')
    expect(review.text()).toContain('已判 12 条')
  })
})

describe('无法核验的拆分', () => {
  it('可信度卡上写出设计边界与可改进各几条，并各自链到那一组主张', async () => {
    const line = (await render()).find('.unverifiable-split')
    expect(line.text()).toContain('设计边界 826')
    expect(line.text()).toContain('可改进 440')
    expect(line.findAll('a').map((link) => link.attributes('href'))).toEqual([
      '/claims?verdict=UNVERIFIABLE&unverifiable_group=boundary',
      '/claims?verdict=UNVERIFIABLE&unverifiable_group=improvable',
    ])
  })
})

describe('请求次数', () => {
  it('不超过三次聚合请求，取代原来的一加每公司一次', async () => {
    await render()
    expect(calls.filter((name) => name !== 'issues')).toHaveLength(3)
  })
})

describe('公司卡片', () => {
  it('整张卡可点，链到该公司的事件台账', async () => {
    // The old cards had no link at all: a reader could see a company's numbers
    // and had no way to reach the events behind them.
    const wrapper = await render()
    const hrefs = wrapper.findAll('.company').map((node) => node.attributes('href'))
    expect(hrefs).toEqual(['/events?company=cxmt', '/events?company=wuxi_apptec'])
  })

  it('没有权威语料的主体明确说出来，而不是显示成核验失败', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('未采集权威披露语料')
  })
})

describe('趋势的诚实边界', () => {
  it('窗口外的条数与最早最晚时间都写出来，不静默裁掉', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('472')
    expect(text).toContain('9')
    expect(text).toContain('2032-01-01')
  })

  it('把晚于今日的时间点名为疑似解析异常', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('疑为发布时间解析异常')
  })
})

describe('原有承诺', () => {
  /* 2026-09-06 按产品决定移除：页面不再自述口径与方法学，说明性文字整体从站内
     撤下（约 4,100 字），这些话改由技术报告与答辩材料承担。删的是断言，不是
     曾经的判断——原句与钉它的理由见 docs/界面体检记录.md §四。
     2026-09-07 补记：导语按产品决定加回来了，但形态换了——每页一句、不超过 40 字、
     只回答「这一页在回答什么问题」，由 ui/conventions.test.js 的「每页一句导语」钉住；
     方法学与「这不是什么」归 /guide 使用指南页，不再摊回每一页。上面撤下的是后者。 */

  it('轮次信息仍与它所指的轮次一起出现', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('当前处理轮次 #10')
    expect(wrapper.text()).toContain('本轮新增事件')
  })
})

describe('纯行情数据不进风险分布', () => {
  it('排除多少条要写在脚注里，而不是悄悄改分母', async () => {
    // 一条曲线的分母悄悄变过，比多一行脚注糟得多——与 out_of_window 同一个理由。
    const wrapper = await render()
    expect(wrapper.text()).toContain('321')
    expect(wrapper.text()).toContain('不计入风险分布')
    expect(wrapper.text()).toContain('仍在事件库与传播图中')
  })
})
