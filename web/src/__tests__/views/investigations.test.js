/** 专项核查工作视图（清单第 3 项）。
 *
 *  * 逾期的排在前面、要用文字说出「逾期」，不只靠颜色；
 *  * 「已逾期」旁边说出「未定截止日」有几条——两件事分开数；
 *  * 与「标注复核」名字不重叠：此前侧栏的「专项核查」指着的恰恰是标注复核那一页。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const row = (id, status, owner, due, overdue) => ({
  alert_id: id, company_id: 'cxmt', headline: `事项 ${id}`, risk_level: 'high',
  adjusted_risk_score: 80, latest_progress: '2026-09-22T08:00:00+00:00',
  disposition: { status, status_label: { investigating: '核查中', reporting: '待上级复核' }[status],
                 owner, due_at: due, overdue },
})

const BOARD = {
  today: '2026-09-23',
  items: [row('late', 'investigating', 'chen', '2026-09-01', true),
          row('soon', 'reporting', 'wang', '2026-10-01', false),
          row('open', 'investigating', null, null, false)],
  summary: {
    total: 3, overdue: 1, no_due: 1,
    by_status: { investigating: { label: '核查中', count: 2 }, reporting: { label: '待上级复核', count: 1 },
                 tracking: { label: '持续跟踪', count: 0 } },
    by_owner: [{ owner: 'chen', count: 1, overdue: 1 }, { owner: 'wang', count: 1, overdue: 0 },
               { owner: null, count: 1, overdue: 0 }],
  },
}
const calls = []

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, api: { riskInvestigations: vi.fn(async (params) => { calls.push(params); return BOARD }) } }
})

import InvestigationsView from '../../views/InvestigationsView.vue'
import { routes } from '../../router.js'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }
async function open() {
  const wrapper = mount(InvestigationsView, { global: { stubs: STUBS } })
  for (let i = 0; i < 3; i += 1) { await new Promise((r) => setTimeout(r, 0)); await wrapper.vm.$nextTick() }
  return wrapper
}

beforeEach(() => { calls.length = 0; window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } })

describe('专项核查', () => {
  it('按服务端给的顺序列出，逾期的那条用文字标出', async () => {
    const wrapper = await open()
    const rows = wrapper.findAll('tbody tr')
    expect(rows[0].text()).toContain('事项 late')
    expect(rows[0].text()).toContain('逾期')
    expect(rows[2].text()).toContain('未定')
  })

  it('逾期与未定截止日分开数', async () => {
    const text = (await open()).text()
    expect(text).toContain('已逾期')
    expect(text).toContain('另有 1 条未定截止日')
  })

  it('按责任人筛，未指派那一栏不能点', async () => {
    const wrapper = await open()
    const buttons = wrapper.findAll('.owners button')
    await buttons.find((b) => b.text().startsWith('wang')).trigger('click')
    await new Promise((r) => setTimeout(r, 0))
    expect(calls.at(-1)).toEqual({ owner: 'wang' })
    expect(buttons.find((b) => b.text().startsWith('未指派')).attributes('disabled')).toBeDefined()
  })

  it('说清楚与标注复核不是一回事，并给出入口', async () => {
    const wrapper = await open()
    expect(wrapper.find('.relation a').attributes('href')).toBe('/review')
  })
})

describe('两页的名字不重叠', () => {
  const byLabel = (label) => routes.filter((r) => r.meta?.nav?.label === label).map((r) => r.path)
  it('专项核查只指向工作视图，标注复核只指向 /review', () => {
    expect(byLabel('专项核查')).toEqual(['/investigations'])
    expect(byLabel('标注复核')).toEqual(['/review'])
  })
})
