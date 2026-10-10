/** M4-6 —「归并错了要能救」。
 *
 *  一个议题就是 (公司, 议题码)，事件落进来是因为标注给它打了那个码。打错了，
 *  台账就是错的，而在此之前页面上没有任何出口。
 *
 *  这里钉住的是这套操作的语义：不勾＝合并，勾一部分＝拆分，同一个原语两种用法；
 *  目标只能从受控词表里选；理由是必填的，因为改派记录是台账为什么长这样的
 *  唯一解释；以及只有 reviewer 能动手。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const ISSUE = {
  issue_id: 'i1', company_id: 'wuxi_apptec', issue_code: 'IP_PATENT', status: 'open',
  event_count: 3, document_count: 3, independent_source_count: 2,
  independent_component_count: 0, risk_score: 82, risk_level: 'high',
  first_seen_at: '2024-03-28', last_seen_at: '2024-10-29',
  event_time_earliest: '2024-02-01', event_time_latest: '2024-05-31',
  manual_event_count: 0, verdicts: { SUPPORTED: 1, UNVERIFIABLE: 8 }, status_history: [],
  timeline: [
    { event_id: 'e1', summary: '第一条', sort_time: '2024-02-01', time_basis: 'event_time', risk_level: 'high',
      source_id: 'fierce_pharma', risk_score: 95, needs_review: 1, reassigned: false },
    { event_id: 'e2', summary: '第二条', sort_time: '2024-03-29', time_basis: 'published_at',
      source_id: 'biospace', risk_score: 95, needs_review: 0, reassigned: false },
    { event_id: 'e3', summary: '第三条', sort_time: '2024-05-03', time_basis: 'event_time',
      source_id: 'fierce_pharma', risk_score: 75, needs_review: 0, reassigned: true },
  ],
}

const LOG = { items: [], active: 0, total: 0 }
const state = { role: 'reviewer', sent: null, log: LOG }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      issue: vi.fn(async () => ISSUE),
      issueAdjustments: vi.fn(async () => state.log),
      issueReassign: vi.fn(async (body) => { state.sent = body; return { moved: 1, issues: 41 } }),
      issueUndoAdjustment: vi.fn(async () => ({ undone: 'a1', issues: 41 })),
    },
  }
})

vi.mock('../../session.js', async (importOriginal) => {
  const actual = await importOriginal()
  // `allows` 是函数体，求值在后；写成对象字面量会在 vi.mock 提升后立刻读 state，
  // 那时它还不存在。视图也只用到 allows。
  return { ...actual, allows: (role) => role === 'reviewer' && state.role === 'reviewer' }
})

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, useRoute: () => ({ params: { id: 'i1' } }) }
})

import IssueDetailView from '../../views/IssueDetailView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }

async function render() {
  const wrapper = mount(IssueDetailView, { global: { stubs: STUBS } })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

async function open(wrapper) {
  await wrapper.findAll('button').find((b) => b.text().includes('改派事件')).trigger('click')
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  state.role = 'reviewer'
  state.sent = null
  state.log = LOG
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

describe('议题人工拆分与合并', () => {
  it('不勾任何事件就是整体合并', async () => {
    const wrapper = await open(await render())
    expect(wrapper.find('form.reassign button[type="submit"]').text()).toBe('整体合并')
  })

  it('勾了事件就变成拆分，并只送出勾中的那些', async () => {
    // 同一个原语的两种用法，差别只在事件集合。
    const wrapper = await open(await render())
    await wrapper.findAll('.pick input')[1].setValue(true)
    await wrapper.find('form.reassign input[type="text"]').setValue('这条讲的是别的')
    expect(wrapper.find('form.reassign button[type="submit"]').text()).toBe('拆分')
    await wrapper.find('form.reassign').trigger('submit')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(state.sent.event_ids).toEqual(['e2'])
    expect(state.sent.from_code).toBe('IP_PATENT')
  })

  it('整体合并送出的 event_ids 是 null，不是全部 id', async () => {
    // 「全选」与「不限定」是两回事：前者会把此刻页面上的集合冻结进请求里，
    // 后者交给服务端按当下的议题成员算。
    const wrapper = await open(await render())
    await wrapper.find('form.reassign input[type="text"]').setValue('两者其实是一条线')
    await wrapper.find('form.reassign').trigger('submit')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(state.sent.event_ids).toBeNull()
  })

  it('没有理由就不能提交', async () => {
    // 改派记录是台账为什么长这样的唯一解释。
    const wrapper = await open(await render())
    expect(wrapper.find('form.reassign button[type="submit"]').attributes('disabled')).toBeDefined()
  })

  it('目标里有「移出台账」，且不含议题自己', async () => {
    // 自绘 listbox：选项展开后才在 DOM 里。
    const wrapper = await open(await render())
    await wrapper.find('form.reassign button.control').trigger('click')
    const labels = wrapper.findAll('form.reassign [role="option"]').map((o) => o.text())
    expect(labels.some((t) => t.includes('移出台账'))).toBe(true)
    expect(labels.some((t) => t.includes('知识产权'))).toBe(false)   // 不能改派给自己
    expect(labels.some((t) => t.includes('技术水平'))).toBe(true)
  })

  it('选「移出台账」时送出的 to_code 是 null', async () => {
    const wrapper = await open(await render())
    await wrapper.find('form.reassign input[type="text"]').setValue('这根本不是一个议题')
    await wrapper.find('form.reassign').trigger('submit')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(state.sent.to_code).toBeNull()
  })

  it('viewer 看不到改派入口', async () => {
    state.role = 'viewer'
    const wrapper = await render()
    expect(wrapper.findAll('button').filter((b) => b.text().includes('改派事件'))).toHaveLength(0)
  })

  it('人工改派过的事件在时间线上有标记', async () => {
    // 归并是模型给的、改派是人给的，两者在事件数里长得一模一样。
    const wrapper = await render()
    expect(wrapper.findAll('.tag.hand')).toHaveLength(1)
  })

  it('生效中的改派列出来，并且能撤销', async () => {
    state.log = { active: 1, total: 1, items: [{
      adjustment_id: 'a1', company_id: 'wuxi_apptec', event_id: 'e9',
      from_code: 'IP_PATENT', to_code: 'TECH_LEVEL', reason: '同一条线',
      actor: 'someone', role: 'reviewer', decided_at: '2026-09-05', undone_at: null }] }
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('人工改派（1 条生效中）')
    expect(text).toContain('同一条线')
    expect(wrapper.findAll('button').some((b) => b.text() === '撤销')).toBe(true)
  })

  it('已撤销的改派不再算作生效', async () => {
    state.log = { active: 0, total: 1, items: [{
      adjustment_id: 'a1', company_id: 'wuxi_apptec', event_id: 'e9',
      from_code: 'IP_PATENT', to_code: 'TECH_LEVEL', reason: '同一条线',
      actor: 'someone', role: 'reviewer', decided_at: '2026-09-05',
      undone_at: '2026-09-05T01:00:00+00:00' }] }
    const wrapper = await render()
    expect(wrapper.text()).not.toContain('人工改派（')
  })
})


describe('专题概况与热度（方案 §6.2）', () => {
  it('详情页说得出独立信源数，并与事件数、文档数分开', async () => {
    const text = (await render()).text()
    expect(text).toContain('独立信源')
    expect(text).toMatch(/事件 \/ 文档\s*3 \/ 3/)
  })

  it('有热度趋势：按月计数，中间没有事件的月份补 0', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('热度趋势')
    const chart = wrapper.findComponent({ name: 'EChart' })
    expect(chart.exists()).toBe(true)
    // 2024-02 到 2024-05 是四个月，其中 2024-04 没有事件，也要在轴上。
    expect(chart.props('tableRows').map((row) => row[0])).toEqual(['2024-02', '2024-03', '2024-04', '2024-05'])
  })
})
