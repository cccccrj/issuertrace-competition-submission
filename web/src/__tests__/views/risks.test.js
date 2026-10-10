/** 风险事项库与风险研判工作台 —— 业务化重组之后这条链路上人能做的事。
 *
 *  这里钉的不是版式，是几条一旦松掉就会把「按业务任务组织」退回原样的规矩：
 *
 *  * 列表里状态不靠颜色单独承载——颜色、图标、文字要同时在场；
 *  * 处置的合法去向由服务端给，界面不自己再实现一遍流程（两份实现迟早各说各话）；
 *  * 无权限的动作**列出并置灰**，不隐藏——看不见一项能力和没有这项能力，在页面上
 *    长得一模一样；
 *  * 终态没有下一步时，要说出为什么，而不是给一排按钮然后全都点不动；
 *  * 研判档案上仍然能回到「命题七项输出」那一页——业务化改名不能把命题契约弄丢。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const ROW = {
  alert_id: 'r1', company_id: 'cxmt', issue_code: 'FUND_RAISE',
  headline: '长鑫科技｜募资：报道与披露口径冲突（3 条核验点）',
  lead_title: '媒体称募资规模上调', summary: '三篇报道给出两个募资口径。',
  risk_level: 'high', adjusted_risk_score: 78.2, credibility: 0.62, credibility_label: 'medium',
  event_count: 3, reasons: [{ code: 'claim_conflict', label: '存在与权威披露直接冲突的主张' }],
  heat: { event_count: 3, independent_sources: 2 },
  latest_progress: '2026-09-20T08:00:00+00:00', updated_at: '2026-09-20T08:00:00+00:00',
  disposition: { status: 'pending', status_label: '待研判', chosen: null, owner: null,
                 recommended: 'clarification', recommended_label: '澄清公告', diverged: false },
}

const DOSSIER = {
  ...ROW,
  subjects: ['长鑫存储'], affected_objects: ['investors_stock_price'],
  phase_label: '问询', phase_coefficient: 1.2,
  window_start: '2026-09-01', window_end: '2026-09-05',
  lead_event_id: 'e1', note: null, recommended_basis: [{ code: 'verdicts', label: '主张裁决', value: '冲突 3' }],
  evidence: [{ claim: '募资规模为 120 亿元', verdict: 'CONFLICT', partial_subtype: null,
               reason: '披露原文为 108 亿元', evidence_title: '招股说明书', evidence_page: 43,
               evidence_section: '募集资金运用', evidence_excerpt: '本次拟募集资金 108 亿元',
               evidence_url: 'https://example.invalid/a.pdf', method: 'llm_adjudicated' }],
  sources: [
    { version_id: 'v1', title: '招股说明书', source_id: 'sse', source_tier: 'S0',
      production_mode: 'ORIGINAL_INVESTIGATION', published_at: '2026-09-01', group: 'authoritative',
      canonical_url: 'https://example.invalid/a.pdf', new_fact_count: 0 },
    { version_id: 'v2', title: '媒体称募资规模上调', source_id: 'cnstock', source_tier: 'S2',
      production_mode: 'REPOST', published_at: '2026-09-03', group: 'relay',
      canonical_url: null, new_fact_count: 1 },
  ],
  propagation: { components: ['c1'],
                 nodes: [{ version_id: 'v1', title: '招股说明书', source_id: 'sse',
                           published_at: '2026-09-01', action: 'ORIGIN', canonical_url: null },
                         { version_id: 'v2', title: '媒体称募资规模上调', source_id: 'cnstock',
                           published_at: '2026-09-03', action: 'REPOST', canonical_url: null }],
                 edges: [{ edge_kind: 'REPOST', from_version_id: 'v1', to_version_id: 'v2' }] },
  changes: [{ diff_id: 'd1', diff_kind: 'numeric', section: '募集资金运用',
              old_value: '108', new_value: '120', right_page: 43 }],
  history: [],
  issue_id: 'i1', opinion: null, returned_by: null,
  conclusions: { confirmed: { label: '风险成立' }, partial: { label: '部分成立' },
                 false_positive: { label: '误报' } },
  similar: [{ alert_id: 'r9', company_id: 'wuxi_apptec', headline: '药明康德｜募资：口径变化',
              risk_level: 'medium', basis: ['同一专题：募资'], same_issue: true,
              disposition: { status_label: '已归档', conclusion_label: '部分成立' } }],
  transitions: [
    { status: 'investigating', label: '核查中', detail: '已发起专项核查', requires: null },
    { status: 'reporting', label: '待上级复核', detail: '上报', requires: null },
    { status: 'archived', label: '已归档', detail: '本轮处置结束', requires: 'reviewer' },
  ],
}

const state = { role: 'analyst', dossier: null, decided: [] }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      risks: vi.fn(async () => ({ total: 1, items: [ROW], limit: 25, offset: 0 })),
      risk: vi.fn(async () => state.dossier),
      reviewers: vi.fn(async () => ({ items: [{ username: 'chen', role: 'reviewer', role_label: '核查负责人' }] })),
      notifyDecide: vi.fn(async (alertId, body) => {
        state.decided.push([alertId, body])
        return { ...state.dossier.disposition, ...body }
      }),
    },
  }
})

vi.mock('../../session.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    session: { ready: true, authenticated: true, username: 'wei', role: 'analyst' },
    allows: (required) => (required === 'reviewer' ? state.role === 'reviewer' : true),
  }
})

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, useRoute: () => ({ query: {}, params: { id: 'r1' } }) }
})

import RisksView from '../../views/RisksView.vue'
import RiskDossierView from '../../views/RiskDossierView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }

async function open(component) {
  const wrapper = mount(component, { global: { stubs: STUBS } })
  for (let i = 0; i < 4; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
    await wrapper.vm.$nextTick()
  }
  return wrapper
}

beforeEach(() => {
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  state.role = 'analyst'
  state.dossier = JSON.parse(JSON.stringify(DOSSIER))
  state.decided = []
})

describe('风险事项库', () => {
  it('一行就能读出公司、等级、可信度、传播热度、处置状态和责任人', async () => {
    const text = (await open(RisksView)).text()
    for (const wanted of ['长鑫科技', '高风险', '0.62', '3', '2 信源', '待研判', '未指派']) {
      expect(text, wanted).toContain(wanted)
    }
  })

  it('处置状态不只有颜色：图标与文字同时在场', async () => {
    // 只靠颜色的状态列，对色觉差异的读者等于空列。
    const state_cell = (await open(RisksView)).find('.state')
    expect(state_cell.text()).toContain('待研判')
    expect(state_cell.text()).toMatch(/[○◐▲◇⊟]/)
  })

  it('默认表头里没有哈希、簇 ID、模型版本这类技术字段', async () => {
    const headers = (await open(RisksView)).findAll('th').map((cell) => cell.text())
    for (const unwanted of ['哈希', '簇', '模型', 'ID']) {
      expect(headers.join(' '), unwanted).not.toContain(unwanted)
    }
  })

  it('核验异常与已归档是页内的视图切换，不再各占侧栏一格', async () => {
    // app-shell.test.js 只核对这两条路由声明了 `meta.entry: 'tab'`；入口真的在这里。
    const tabs = (await open(RisksView)).findAll('.view-tabs a')
    expect(tabs.map((tab) => tab.text())).toEqual(['全部', '待研判', '核验异常', '处理中', '持续跟踪', '已归档'])
    expect(tabs.map((tab) => tab.attributes('href'))).toContain('/risks/anomalies')
    expect(tabs.map((tab) => tab.attributes('href'))).toContain('/risks/archived')
    expect(tabs.filter((tab) => tab.classes().includes('on')).map((tab) => tab.text())).toEqual(['全部'])
  })

  it('每一行有一个明确的「查看事项」，而不只是展开箭头', async () => {
    const link = (await open(RisksView)).findAll('a').find((node) => node.text().includes('查看事项'))
    expect(link.attributes('href')).toBe('/risks/r1')
  })

  it('事项标题之外仍然给得出首发报道的标题', async () => {
    // 标题改成平台自己命名之后，原文标题不能就此消失——它是回溯到原件的入口。
    expect((await open(RisksView)).text()).toContain('媒体称募资规模上调')
  })
})

/** 修改思路 v2 §四—§六：研判档案改成工作台。左边四段是依据，右边「人工研判」是这一页
 *  存在的目的——写结论、定级、选下一步，不必离开这一页。 */
describe('风险研判工作台', () => {
  const click = async (wrapper, selector, text) => {
    const node = wrapper.findAll(selector).find((item) => item.text().includes(text))
    expect(node, `${selector} 里没有「${text}」`).toBeTruthy()
    await node.trigger('click')
    for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
    return node
  }

  it('四段依据加右栏人工研判，都在同一页上', async () => {
    const wrapper = await open(RiskDossierView)
    for (const id of ['overview', 'evidence', 'context', 'history', 'decide']) {
      expect(wrapper.find(`#${id}`).exists(), id).toBe(true)
    }
    expect(wrapper.text()).toContain('风险研判工作台')
    expect(wrapper.findAll('.anchors a')).toHaveLength(4)
  })

  it('每条核验结论都能定位到原文的第几页', async () => {
    const text = (await open(RiskDossierView)).text()
    expect(text).toContain('招股说明书')
    expect(text).toContain('43')
  })

  it('I-05：来源全是媒体、也没有一条对过披露时，醒目说明结论只来自二手报道', async () => {
    state.dossier.source_basis = { kind: 'secondhand_only', authoritative_sources: 0,
                                   media_sources: 1, checked_claims: 0, filings_cited: [] }
    const text = (await open(RiskDossierView)).text()
    expect(text).toContain('本事项完全依据二手报道')
  })

  it('I-05：来源全是媒体、但事实已对过披露原文时，不说「没有来源」，而说对过哪几份', async () => {
    state.dossier.source_basis = { kind: 'secondhand_checked', authoritative_sources: 0,
                                   media_sources: 2, checked_claims: 1, filings_cited: ['招股说明书'] }
    state.dossier.sources = state.dossier.sources.filter((item) => item.group !== 'authoritative')
    const text = (await open(RiskDossierView)).text()
    expect(text).toContain('1 条事实已与权威披露原文核对')
    expect(text).toContain('《招股说明书》')
    expect(text).toContain('来源文章里没有交易所、监管或公司公告')
    expect(text).not.toContain('本事项完全依据二手报道')
  })

  it('I-05：每条来源给出发布方域名；标题缺失时用网址兜底而不是「无标题」', async () => {
    state.dossier.sources[1] = { ...state.dossier.sources[1], title: null, domain: 'tomshardware.com',
                                 canonical_url: 'https://www.tomshardware.com/pc-components/dram/chinas-cxmt-targets-30-percent' }
    const text = (await open(RiskDossierView)).text()
    expect(text).toContain('tomshardware.com')
    expect(text).toContain('chinas-cxmt-targets-30-percent')
    expect(text).not.toContain('（无标题）')
  })

  it('下一步来自服务端给的合法去向，按钮说的是动作而不是状态名', async () => {
    const labels = (await open(RiskDossierView)).findAll('.step').map((node) => node.text())
    expect(labels).toEqual(['发起专项核查', '上报复核', expect.stringContaining('确认归档')])
  })

  it('无权限的动作列出来并置灰，而不是藏起来', async () => {
    const wrapper = await open(RiskDossierView)
    const archive = wrapper.findAll('.step').find((node) => node.text().includes('确认归档'))
    expect(archive.attributes('disabled')).toBeDefined()
    expect(archive.text()).toContain('核查负责人')
  })

  it('有权限时选中一步再确认，状态才送出去', async () => {
    state.role = 'reviewer'
    const wrapper = await open(RiskDossierView)
    await click(wrapper, '.step', '确认归档')
    expect(state.decided).toHaveLength(0)            // 选中不等于提交
    await click(wrapper, '.step-form .primary', '确认归档')
    expect(state.decided[0][1].status).toBe('archived')
  })

  it('上报前没有结论就不让提交，并说出缺什么', async () => {
    const wrapper = await open(RiskDossierView)
    await click(wrapper, '.step', '上报复核')
    const submit = wrapper.find('.step-form .primary')
    expect(submit.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('上报前先在上方给出人工结论')
  })

  it('有了结论，上报把结论与上报对象一起送出', async () => {
    const wrapper = await open(RiskDossierView)
    wrapper.vm.judgement.conclusion = 'partial'
    await click(wrapper, '.step', '上报复核')
    await click(wrapper, '.step-form .primary', '上报复核')
    const body = state.decided[0][1]
    expect(body).toMatchObject({ status: 'reporting', conclusion: 'partial', owner: 'chen' })
  })

  it('确认系统判断停在当前状态，结论记为风险成立、定级跟随系统', async () => {
    const wrapper = await open(RiskDossierView)
    await click(wrapper, '.move', '确认系统判断')
    expect(state.decided[0][1]).toMatchObject(
      { status: 'pending', conclusion: 'confirmed', level_override: null })
  })

  it('标记误报是一次结论，不是一次归档', async () => {
    const wrapper = await open(RiskDossierView)
    await click(wrapper, '.move', '标记误报')
    expect(state.decided[0][1]).toMatchObject({ status: 'pending', conclusion: 'false_positive' })
  })

  it('没人管的待研判事项可以认领，认领不替它选口径', async () => {
    const wrapper = await open(RiskDossierView)
    await click(wrapper, '.move', '认领')
    expect(state.decided[0][1]).toEqual({ status: 'pending', owner: 'wei' })
  })

  it('退回补证时，上级的退回意见摆在最上面', async () => {
    state.dossier.disposition = { ...state.dossier.disposition, status: 'returned', status_label: '退回补证' }
    state.dossier.returned_by = { actor: 'chen', note: '缺公司正式回应', occurred_at: '2026-09-22' }
    const text = (await open(RiskDossierView)).text()
    expect(text).toContain('上级退回意见')
    expect(text).toContain('缺公司正式回应')
  })

  it('已上报的事项在上级手里：分析师看得见结论，改不了', async () => {
    state.dossier.disposition = { ...state.dossier.disposition, status: 'reporting',
                                  status_label: '待上级复核', owner: 'chen' }
    const wrapper = await open(RiskDossierView)
    const confirm = wrapper.findAll('.move').find((node) => node.text().includes('确认系统判断'))
    expect(confirm.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('正等 chen 复核')
  })

  it('终态说得出为什么没有下一步，而不是给一排点不动的按钮', async () => {
    state.dossier.transitions = []
    const wrapper = await open(RiskDossierView)
    expect(wrapper.findAll('.step')).toHaveLength(0)
    expect(wrapper.text()).toContain('已归档是终态')
  })

  it('相似事项写明为什么相似，并给出当时的结论', async () => {
    const item = (await open(RiskDossierView)).find('.similar li')
    expect(item.text()).toContain('同一专题：募资')
    expect(item.text()).toContain('结论：部分成立')
    expect(item.find('a').attributes('href')).toBe('/risks/r9')
  })

  it('多源信息分三组，转载不与权威披露混在一起', async () => {
    const text = (await open(RiskDossierView)).text()
    expect(text).toContain('权威披露')
    expect(text).toContain('独立报道')
    expect(text).toContain('传播与转述')
    // 「独立信源」是这个量的唯一名字。
    expect(text).not.toContain('独立佐证')
  })

  it('传播脉络默认是一条时间线，首发是读出来的', async () => {
    const items = (await open(RiskDossierView)).findAll('.spread li')
    expect(items).toHaveLength(2)
    expect(items[0].text()).toContain('首发')
    expect(items[1].text()).toContain('转载')
  })

  it('还留着回到「命题七项输出」那一页的路', async () => {
    // 业务化改名不能把命题契约弄丢：①–⑦ 仍在 /events/:id 上。
    const wrapper = await open(RiskDossierView)
    const link = wrapper.findAll('a').find((node) => node.attributes('href') === '/events/e1')
    expect(link).toBeTruthy()
    expect(link.text()).toContain('完整分析')
  })

  it('可信度与系统抽取置信度被说成两个数，不合成一个', async () => {
    expect((await open(RiskDossierView)).text())
      .toContain('与系统对自身抽取结果的置信度是两个数')
  })

  it('这一页能导出一份可下发的简报', async () => {
    // 演示动线第 6 步要的输出物。链接直接指向 Word，不经过打印页。
    const wrapper = await open(RiskDossierView)
    const link = wrapper.findAll('a')
      .find((node) => node.attributes('href') === '/api/reports/risks/r1.docx')
    expect(link).toBeTruthy()
    expect(link.text()).toContain('简报')
  })
})
