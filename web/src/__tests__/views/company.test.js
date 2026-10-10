/** 公司档案（清单第 4 项）。验收：档案页读得出未结事项数；名字从接口来，页面不写任何一家公司。 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const DOSSIER = {
  company_id: 'x1', name: '甲公司', canonical_name: '甲公司股份有限公司',
  stock_codes: [{ exchange: 'SSE', code: '600000' }],
  open: { total: 11, high: 4, overdue: 1, by_status: [],
          items: [{ alert_id: 'r1', headline: '甲公司｜募资：口径冲突', risk_level: 'high',
                    adjusted_risk_score: 90, latest_progress: '2026-09-20T00:00:00Z',
                    disposition: { status_label: '待研判' } }] },
  trend: [{ month: '2026-06', high: 1, medium: 0, low: 0 }, { month: '2026-08', high: 0, medium: 2, low: 1 }],
  disclosures: { total: 37, items: [{ version_id: 'v1', title: '招股说明书（注册稿）', published_at: '2026-05-01',
                                      source_id: 'sse', canonical_url: null }] },
  issues: [{ issue_id: 'i1', issue_code: 'FUND_RAISE', issue_label: '募资', risk_level: 'high',
             event_count: 12, independent_source_count: 5 }],
  related: [{ org_id: 'csrc', name: '中国证券监督管理委员会', events: 7 }],
}
const calls = []

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, api: { company: vi.fn(async (id) => { calls.push(id); return DOSSIER }) } }
})
vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, useRoute: () => ({ params: { id: 'x1' }, query: {} }) }
})

import CompanyView from '../../views/CompanyView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' }, EChart: true }
async function open() {
  const wrapper = mount(CompanyView, { global: { stubs: STUBS } })
  for (let i = 0; i < 3; i += 1) { await new Promise((r) => setTimeout(r, 0)); await wrapper.vm.$nextTick() }
  return wrapper
}

beforeEach(() => { calls.length = 0; window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } })

describe('公司档案', () => {
  it('读得出未结事项数，并且数字点得进去', async () => {
    const wrapper = await open()
    expect(calls).toEqual(['x1'])
    expect(wrapper.text()).toContain('未结事项')
    expect(wrapper.text()).toContain('11')
    expect(wrapper.find('a[href="/risks?company=x1"]').exists()).toBe(true)
  })

  it('名字、全称、股票代码都是接口给的', async () => {
    const text = (await open()).text()
    expect(text).toContain('甲公司')
    expect(text).toContain('甲公司股份有限公司')
    expect(text).toContain('600000')
  })

  it('五块都在：趋势、未结事项、重点专题、关联主体、权威披露', async () => {
    const wrapper = await open()
    const text = wrapper.text()
    for (const heading of ['风险趋势', '未结事项', '重点专题', '关联主体', '最新权威披露']) expect(text).toContain(heading)
    expect(wrapper.find('a[href="/issues/i1"]').exists()).toBe(true)
    expect(text).toContain('中国证券监督管理委员会')
  })

  it('在公司自己的档案页上，事项标题不再以公司名开头', async () => {
    const link = (await open()).find('a[href="/risks/r1"]')
    expect(link.text()).toBe('募资：口径冲突')
  })

  it('只显示了一部分时给出「查看全部」', async () => {
    expect((await open()).text()).toContain('查看全部 11 条')
  })
})
