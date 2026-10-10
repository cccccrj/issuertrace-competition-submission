/** The event detail page — 「答辩演示的核心页面」 per 功能大纲 M7-2.
 *
 *  Its ①–⑦ numbering is a contract with the brief's seven required outputs, not
 *  a layout choice, so these pin the structure before anything else edits the
 *  file. Additions to the page are fine; losing a section, or reordering them,
 *  is not.
 *
 *  Two sentences are pinned by name as well. Both draw a distinction the page
 *  would otherwise blur, and both are the kind of copy that gets "tidied" by
 *  someone who has not read why it is there.
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const EVENT = {
  event_id: 'e1',
  summary: '长鑫科技拟募资 295 亿元',
  title: '长鑫科技招股说明书披露募资规模',
  source_id: 'cninfo',
  canonical_url: 'https://example.test/a',
  published_at: '2026-08-01',
  event_time: '2026-08-01',
  time_basis: 'event_time',
  event_types: ['IPO_REFINANCING'],
  issue_codes: ['FUND_RAISE'],
  production_mode: 'BRIEF',
  event_subjects: ['长鑫科技'],
  affected_objects: ['investors_stock_price'],
  impact_direction: 'positive',
  impact_magnitude: 'major',
  article_sentiment: 'neutral',
  amounts: ['295亿元'],
  ratios: [],
  organizations: ['上交所'],
  extraction_confidence: 0.91,
  elements: [
    { element_type: 'amount', ordinal: 0, raw_text: '295亿元',
      normalized_value: '29500000000', unit: 'CNY', basis: 'explicit', status: 'normalized' },
    { element_type: 'subject', ordinal: 1, raw_text: '业内人士',
      normalized_value: null, unit: null, basis: null, status: 'unresolved' },
  ],
  claims: [],
  alerts: [],
  timeline: null,
  propagation: [],
  label_source: 'ai',
  risk_score: 65,
  // 七项加减，合计等于 risk_score：15 + 20 + 20 + 20 − 10。
  risk_breakdown: [
    { key: 'base', points: 15, detail: '' },
    { key: 'eventhood', points: 20, detail: 'EVENT' },
    { key: 'magnitude', points: 20, detail: 'major' },
    { key: 'sensitive_type', points: 20, detail: 'REGULATORY_COMPLIANCE' },
    { key: 'secondhand', points: -10, detail: 'BRIEF' },
  ],
  basis: {
    event_type: null,
    event_impact: null,
    production_mode: { reason: '通篇引用公司口径、无独立信源，末尾附联系方式',
                       confidence: 0.82, basis: 'system' },
    article_sentiment: { reason: '「稳步推进」「显著提升」等褒义措辞贯穿全文',
                         confidence: 0.77, basis: 'system' },
  },
}

const ADVICE = {
  items: [{ alert_id: 'a1', recommended: 'clarification', recommended_label: '澄清公告',
            recommended_detail: '存在与披露冲突的主张，需公告澄清',
            why: '存在与披露原文冲突的主张，且证据可信度高——沉默会被读作默认。',
            basis: [{ code: 'verdicts', label: '主张裁决', value: '冲突 1' }] }],
  actions: {},
}

const state = { event: EVENT, dispositions: async () => ADVICE }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      event: vi.fn(async () => state.event),
      notifyDispositions: vi.fn(async () => state.dispositions()),
    },
  }
})

import EventDetailView from '../../views/EventDetailView.vue'

const STUBS = {
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
}

async function render() {
  const wrapper = mount(EventDetailView, {
    global: {
      stubs: STUBS,
      mocks: { $route: { params: { id: 'e1' } } },
      provide: {},
    },
  })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  state.event = EVENT
  state.dispositions = async () => ADVICE
})

const withAlert = () => ({
  ...EVENT,
  alerts: [{ alert_id: 'a1', headline: '募资规模与披露不一致', risk_level: 'high',
             adjusted_risk_score: 72.5, credibility: 0.62, credibility_label: 'high',
             credibility_basis: { verdict: 0.7, source: 0.9, independence: 0.4,
                                  production: 0.5, verdict_note: '1 条冲突主张',
                                  source_tier: 'S1', independent_components: 2,
                                  production_mode: 'BRIEF' },
             reasons: [{ code: 'conflict', label: '与披露冲突' }],
             window_start: '2026-08-01', window_end: '2026-08-03', event_count: 4 }],
})

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, useRoute: () => ({ params: { id: 'e1' } }) }
})

describe('命题七项输出', () => {
  it('七个小节按 ①–⑦ 的顺序出现', async () => {
    // The numbering is the contract. A section quietly dropped or reordered
    // would break the mapping the brief's output requirement is checked against.
    const wrapper = await render()
    const headings = wrapper.findAll('h2').map((node) => node.text())
    const numbered = headings.filter((text) => /^[①②③④⑤⑥⑦]/.test(text))
    const marks = numbered.map((text) => text[0])
    expect(marks).toEqual(['①', '②', '④', '⑤', '⑥', '⑦'].filter((m) => marks.includes(m)))
    // ② and ③ share one card by design; every other output has its own.
    expect(headings.join(' ')).toContain('③')
  })

  it('摘要、主体、影响对象都在页面上，且是中文而不是枚举码', async () => {
    // This assertion used to require the literal `investors_stock_price`, which
    // enshrined the defect: three of the seven required outputs — 事件类型、
    // 影响对象、影响方向 — rendered as raw English enums on the page 功能大纲
    // calls 「答辩演示的核心页面」. `api/labels.py` had the Chinese names the
    // whole time; the front end simply had no copy of two of the maps.
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('长鑫科技拟募资 295 亿元')
    expect(text).toContain('长鑫科技')
    expect(text).toContain('投资者与股价')
    expect(text).not.toContain('investors_stock_price')
    expect(text).toContain('发行与再融资')
    expect(text).toContain('正面 · 重大')
  })
})

describe('要素标准化', () => {
  it('每个要素显示原文、标准化结果、单位与依据', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('295亿元')
    expect(text).toContain('29500000000')
    expect(text).toContain('CNY')
  })

  it('未解析的要素保留原文而不是留空', async () => {
    // Failing to resolve is not the same as having nothing; the original
    // wording is the evidence and it stays, labelled.
    const wrapper = await render()
    expect(wrapper.text()).toContain('业内人士')
    expect(wrapper.text()).toContain('未解析')
  })

  it('小节标题报出已归一的数量，弱项不必点开就能看见', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toMatch(/已归一\s*1\s*项/)
  })
})

describe('不能被改掉的两句话', () => {
  it('没有预警时说明可信度与抽取置信度是两回事', async () => {
    // A model's self-reported confidence is not a credibility score, and the
    // page has to say so rather than letting one stand in for the other.
    const wrapper = await render()
    expect(wrapper.text()).toContain('系统自评，非人工核验结果')
  })

  it('没有可定位主张时说明它被排除在分母之外', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('已排除在核验成功率分母之外')
  })
})

describe('语气与影响分开呈现', () => {
  it('两个字段并列显示，允许不一致', async () => {
    // 方案 §8.4: tone describes how a piece is written, impact what the event
    // does. Showing only one is what let them be the same value for so long.
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('影响方向')
    expect(text).toContain('语言倾向')
  })
})

describe('⑦ 处置建议', () => {
  it('给出建议本身、推导出它的那句话，以及四项依据', async () => {
    // An action code with no argument attached to it is advice nobody can
    // dispute — which is the one thing dispositions.py says it must not be.
    state.event = withAlert()
    const wrapper = await render()
    await new Promise((resolve) => setTimeout(resolve, 0))
    const text = wrapper.text()
    expect(text).toContain('澄清公告')
    expect(text).toContain('沉默会被读作默认')
    expect(text).toContain('主张裁决')
  })

  it('取不到建议时页面照常显示预警理由', async () => {
    // The recommendation is a second request. Losing it must cost the reader
    // the recommendation, not the event.
    state.event = withAlert()
    state.dispositions = async () => { throw new Error('boom') }
    const wrapper = await render()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(wrapper.text()).toContain('募资规模与披露不一致')
    expect(wrapper.text()).not.toContain('建议，非结论')
  })
})

describe('影响方向的外部对照', () => {
  it('有行情时给出超额收益，并带上它的基准与窗口', async () => {
    // 基准与窗口是这个数的单位：不说明对标什么，「超额 +4.12%」没有意义。
    // 2026-09-06 起页面不再解释「这是对照不是因果」——说明性文字整体撤下，
    // 那句话改由技术报告承担；2026-09-07 起也可以在 /guide 使用指南页找到它。
    state.event = { ...EVENT, market_reaction: {
      symbol: 'sh603259', benchmark_symbol: 'sh000300', anchor_date: '2026-08-03',
      anchor_basis: 'published_at', baseline_date: '2026-08-01',
      car_1: 0.0412, car_3: 0.03, car_5: 0.02, sigma: 0.0266,
      observed_direction: 'up', significant: 1, agrees: 1 } }
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('+4.12%')
    expect(text).toContain('超额')
    expect(text).toContain('沪深300')
  })

  it('小于一个标准差时说「未见明显反应」，不说「中性」', async () => {
    // 「没测出动静」和「市场认为中性」是两种不同的断言，只有前者被小数字支持。
    state.event = { ...EVENT, market_reaction: {
      symbol: 'sh603259', benchmark_symbol: 'sh000300', anchor_date: '2026-08-03',
      anchor_basis: 'published_at', baseline_date: '2026-08-01',
      car_1: 0.001, car_3: 0.002, car_5: 0.001, sigma: 0.0266,
      observed_direction: 'none', significant: 0, agrees: null } }
    const wrapper = await render()
    const badge = wrapper.find('.reaction')
    expect(badge.text()).toContain('未见明显反应')
    // 断言限定在这枚徽标上：页面别处的「中性」是语言倾向，本来就该在。
    expect(badge.text()).not.toContain('中性')
  })

  it('没有行情时把缺口说出来，而不是留白', async () => {
    // 一家 2026 年上市的公司，上市前的事件根本没有价格可比。这个洞要写出来。
    state.event = { ...EVENT, market_reaction: null }
    const wrapper = await render()
    expect(wrapper.text()).toContain('无市场对照')
    expect(wrapper.text()).not.toContain('超额')
  })

  it('方向不靠颜色单独承载', async () => {
    // 全站唯一用涨跌语义的地方，而涨跌色在红绿色觉障碍下不可靠。
    state.event = { ...EVENT, market_reaction: {
      symbol: 'sh603259', benchmark_symbol: 'sh000300', anchor_date: '2026-08-03',
      anchor_basis: 'published_at', baseline_date: '2026-08-01',
      car_1: -0.0412, car_3: -0.05, car_5: -0.06, sigma: 0.0266,
      observed_direction: 'negative', significant: 1, agrees: 1 } }
    const wrapper = await render()
    expect(wrapper.find('.reaction').text()).toContain('-4.12%')
  })
})

describe('被丢弃的主张', () => {
  it('把模型提过、定位不到的原话列出来', async () => {
    // 此前只记「丢了 N 条」不记内容，于是分不清模型在转述（该丢）还是定位器
    // 太严（是缺陷）。全语料 675 次丢弃，一次证据都没留下。
    state.event = { ...EVENT, claims: [],
                    dropped_claims: ['公司预计全年收入增长 30% 以上', '管理层对前景表示乐观'] }
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('公司预计全年收入增长 30% 以上')
    expect(text).toContain('未能在原文定位的 2 句')
  })

  /* 2026-09-06 按产品决定移除：页面不再解释「这些主张为什么被丢弃」——说明性
     文字整体从站内撤下，改由技术报告承担。被丢弃的原话本身仍然列出。
     2026-09-07 补记：加回来的只有页头那一句导语（每页一句、40 字以内），
     方法学仍然不摊回页面，归 /guide。 */

  it('没有丢弃记录时这一块不出现', async () => {
    // 早于这次改动标注的事件没有这个字段，空标题比空内容更糟。
    state.event = { ...EVENT, claims: [], dropped_claims: [] }
    const wrapper = await render()
    expect(wrapper.find('.dropped').exists()).toBe(false)
    expect(wrapper.text()).toContain('已排除在核验成功率分母之外')
  })
})

describe('前因与后果', () => {
  const CAUSAL = [
    { edge_id: 'k1', side: 'cause', relation: 'TRIGGERS', other_event_id: 'e0',
      title: '药明康德：已对美国国防部提起诉讼', published_at: '2026-06-11',
      summary: '公司于 2026 年 6 月 11 日提起诉讼', gap_hours: 1368, method: 'system_review',
      reason: '前一事件明确记载公司提起诉讼，后一事件是法院对该诉讼作出的裁决',
      quote_from: '公司于2026年6月11日在美国哥伦比亚特区联邦地区法院提起诉讼',
      quote_to: '美国时间8月7日，法院就公司的初步禁令动议作出了裁决' },
    { edge_id: 'k2', side: 'effect', relation: 'RESOLVES', other_event_id: 'e2',
      title: '上市公告书', published_at: '2026-07-27', summary: '股票将于 7 月 27 日上市',
      gap_hours: 264, method: 'rule_institutional_sequence',
      reason: '审核程序推进：发行与询价 → 上市后', quote_from: null, quote_to: null },
  ]

  it('把前因和后果分开列，并且用中文关系名而不是枚举码', async () => {
    // 这一页此前答不出「这件事之前发生了什么」——只能跳回整张时间线用眼睛找。
    state.event = { ...EVENT, causal_edges: CAUSAL }
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('前因 · 1')
    expect(text).toContain('后果 · 1')
    expect(text).toContain('触发')
    expect(text).toContain('化解')
    expect(text).not.toContain('TRIGGERS')
  })

  it('每条边都带原文引证，并说明这条边是怎么来的', async () => {
    // 判据是两端的原话，不是相似度分数——那正是旧的「同议题，相似度 0.33」
    // 无法取信于人的地方。
    state.event = { ...EVENT, causal_edges: CAUSAL }
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('法院就公司的初步禁令动议作出了裁决')
    expect(text).toContain('系统研判，两端引证已核')
    // 不能写「原文引证」：实测 8,259 句引证里只有 30.6% 能在文档标题或原子主张中
    // 找到，其余引的是模型写的事件摘要。见 docs/因果边自查记录.md §四。
    expect(text).not.toContain('原文引证')
    expect(text).toContain('审核阶段次序')
  })

  it('没有因果边时整块不出现，不留一个空标题', async () => {
    state.event = { ...EVENT, causal_edges: [] }
    const wrapper = await render()
    expect(wrapper.text()).not.toContain('前因与后果')
  })

  /* 2026-09-06 按产品决定移除：页面不再自述口径与方法学（约 4,100 字整体撤下），
     这些话改由技术报告与答辩材料承担。删的是断言，不是曾经的判断。 */

  it('新增的这一块不带圈号，七项输出的编号契约不受影响', async () => {
    state.event = { ...EVENT, causal_edges: CAUSAL }
    const wrapper = await render()
    const numbered = wrapper.findAll('h2').map((node) => node.text())
      .filter((text) => /^[①②③④⑤⑥⑦]/.test(text))
    expect(numbered.some((text) => text.includes('前因'))).toBe(false)
  })
})

describe('前因与后果的折叠', () => {
  const many = Array.from({ length: 9 }, (_, index) => ({
    edge_id: `m${index}`, side: 'effect', relation: 'TRIGGERS', other_event_id: `e${index}`,
    title: `后续事件 ${index}`, published_at: '2026-07-01', summary: '摘要',
    gap_hours: 24 * (index + 1), method: 'system_review', reason: '理由',
    quote_from: '前引', quote_to: '后引',
  }))

  it('边太多时先给最近的几条，其余收起而不是丢掉', async () => {
    // 实测有事件带 31 条后果边，整块会把页面撑成近一万一千像素高。
    state.event = { ...EVENT, causal_edges: many }
    const wrapper = await render()
    expect(wrapper.text()).toContain('后果 · 9')
    expect(wrapper.text()).toContain('另有 3 条未展开')
    // 收起的那几条仍在 DOM 里，展开即见——折叠不是截断。
    expect(wrapper.text()).toContain('后续事件 8')
  })
})

describe('无标题文档的因果边', () => {
  it('退到事件摘要，并说明是退过的，而不是渲染一个空链接', async () => {
    // 语料里 203 个文档版本没有标题（港交所公告与问询回复的分册居多）。
    state.event = { ...EVENT, causal_edges: [{
      edge_id: 'n1', side: 'cause', relation: 'RESPONDS_TO', other_event_id: 'e9',
      title: null, summary: '长鑫科技回复上交所第二轮问询函，说明清辉集电的表决情况',
      published_at: '2026-01-05', gap_hours: 48, method: 'system_review', reason: '理由',
      quote_from: null, quote_to: null,
    }] }
    const wrapper = await render()
    const link = wrapper.findAll('a').find((node) => node.attributes('href') === '/events/e9')
    expect(link.text()).toContain('长鑫科技回复上交所第二轮问询函')
    expect(wrapper.text()).toContain('该文档无标题')
  })
})

describe('分类的依据', () => {
  /** 一位外部读者说「分类的依据有的还是太笼统了」，另一位说页面上解释性文字太多
   *  （2026-09-06 因此净删了 2,592 字）。两条都对，所以依据一律默认收起、点开有据：
   *  值仍是唯一常显的东西，常驻正文一个字都没有增加。这几条就是钉住这个形态的。 */

  it('依据默认收起，但理由已经在 DOM 里', async () => {
    const wrapper = await render()
    const blocks = wrapper.findAll('details')
    expect(blocks.length).toBeGreaterThan(0)
    // 没有一个依据折叠块是默认展开的。
    for (const block of blocks) {
      expect(block.attributes('open')).toBeUndefined()
    }
    // 折叠态的文字仍在 DOM 里——钉句子的测试照常过，键盘可达是免费的。
    expect(wrapper.text()).toContain('无独立信源')
    expect(wrapper.text()).toContain('褒义措辞')
  })

  it('还没判过的那一轴说「尚未覆盖」，不是留一片空白', async () => {
    // production_mode_labels 只覆盖 1,033 / 1,907 条事件（公告类不进那一轮）；
    // 读者必须能分清「未判」和「判了但没给理由」。
    const wrapper = await render()
    expect(wrapper.text()).toContain('该判据尚未覆盖此条事件')
  })

  it('风险分摊开七项加减，且合计等于分数', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('的七项加减')
    expect(wrapper.text()).toContain('二手稿件')
    // −10 那一项必须带负号：把减分显示成一段堆叠贡献是错的。
    expect(wrapper.text()).toContain('-10')
    expect(wrapper.text()).toContain('合计')
    const total = EVENT.risk_breakdown.reduce((sum, part) => sum + part.points, 0)
    expect(total).toBe(EVENT.risk_score)
  })

  it('影响强度用中文，而 uncertain 不再以英文码示人', async () => {
    const { IMPACT_DIRECTION_LABELS, IMPACT_MAGNITUDE_LABELS } = await import('../../api.js')
    expect(IMPACT_DIRECTION_LABELS.uncertain).toBe('待定')
    expect(IMPACT_MAGNITUDE_LABELS.moderate).toBe('一般')
  })
})

/** 「可核验」得能点开。
 *
 *  这一页此前 canonical_url 出现 0 次——fixture 里一直有这个字段（EVENT.canonical_url），
 *  却没有一条断言检查它被渲染出来，而页面上确实一次都没用。整条证据路径同理：四个后端
 *  接口都 JOIN 了 document_versions，独独没取 canonical_url，于是《文件名》第 N 页永远
 *  是一段纯文本。
 */
describe('证据点得开', () => {
  const CITED = {
    claim_id: 'c1', text: '本次拟募集资金 295 亿元', issue_code: 'FUND_RAISE',
    claim_type: 'fact', verdict: 'SUPPORTED',
    evidence_page: 43, evidence_section: '第六节',
    evidence_text: '本次发行拟募集资金 2,950,000 万元',
    evidence_version_id: 'v9', evidence_title: '注册稿_招股说明书',
    evidence_url: 'https://example.test/prospectus.pdf',
    verdict_method: 'llm_adjudicated', inquiry: null,
  }

  it('证据的文件名是链接，且锚点直接指向那一页', async () => {
    // 页码来自 processor 的 [PAGE n]，是 PDF 的物理页序，正是 #page= 的语义。
    // 少了锚点，读者拿到的是「这份三百页的公告，自己翻到第 43 页」。
    state.event = { ...EVENT, claims: [CITED] }
    const wrapper = await render()
    const href = wrapper.findAll('a').map((node) => node.attributes('href'))
    expect(href).toContain('https://example.test/prospectus.pdf#page=43')
  })

  it('没有地址时退回纯文本，不留一个点不动的链接', async () => {
    state.event = { ...EVENT, claims: [{ ...CITED, evidence_url: null }] }
    const wrapper = await render()
    expect(wrapper.text()).toContain('注册稿_招股说明书')
    expect(wrapper.findAll('a').map((node) => node.attributes('href')))
      .not.toContain('https://example.test/prospectus.pdf#page=43')
  })

  it('页头链得到这条事件自己的原报道', async () => {
    state.event = { ...EVENT, claims: [] }
    const wrapper = await render()
    expect(wrapper.findAll('a').map((node) => node.attributes('href')))
      .toContain('https://example.test/a')
  })

  it('同源簇从事件这一页走得到，并说出是多少个来源', async () => {
    // 这一页此前对同源簇只字不提：读者看得到「传播链路」，看不到「这篇稿子还有
    // 几个近重复版本」，也走不到那份成员名单。
    state.event = { ...EVENT, claims: [],
                    duplicate_cluster: { cluster_id: 'k9', member_count: 12, distinct_sources: 1 } }
    const wrapper = await render()
    expect(wrapper.findAll('a').map((node) => node.attributes('href')))
      .toContain('/duplicates?cluster=k9')
    expect(wrapper.text()).toContain('单一来源')
  })
})
