/** 我的任务（修改思路 v2 §七）。
 *
 *  钉的是这一页此前做不到的三件事：说得出是什么任务、谁交来的、要我做什么；
 *  按钮随任务类型变；个人任务清空时指回公共队列，而不是只说「没有」。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const KINDS = {
  returned: { label: '退回补证', action: '补充核验', detail: '上级退回，按退回意见补证后再上报' },
  reporting: { label: '上级复核', action: '查看并复核', detail: '下级已给出结论并上报给你' },
  investigating: { label: '专项核查', action: '进入核查', detail: '你负责的专项核查' },
  pending: { label: '待研判', action: '进入研判', detail: '已分派给你，还没给出结论' },
  tracking: { label: '持续跟踪', action: '查看新进展', detail: '结论已定，按复核日回来看' },
}
const item = (id, kind, handed = null, extra = {}) => ({
  alert_id: id, company_id: 'cxmt', headline: `长鑫科技｜事项 ${id}`, risk_level: 'high',
  adjusted_risk_score: 70,
  disposition: { status: kind, due_at: null, overdue: false, conclusion_label: null, ...extra },
  task: { kind, ...KINDS[kind] }, handed_by: handed,
})

const state = { body: null }
vi.mock('../../api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  api: { tasks: vi.fn(async () => state.body) },
}))

import TasksView from '../../views/TasksView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }
async function open() {
  const wrapper = mount(TasksView, { global: { stubs: STUBS } })
  for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
  return wrapper
}

beforeEach(() => {
  state.body = {
    kinds: KINDS, unclaimed: 12,
    counts: { returned: 1, reporting: 0, investigating: 0, pending: 1, tracking: 0 },
    items: [
      item('a1', 'returned', { actor: 'chen', note: '缺公司正式回应', occurred_at: '2026-09-22' },
           { conclusion_label: '证据不足' }),
      item('a2', 'pending'),
    ],
  }
})

describe('我的任务', () => {
  it('每条说得出任务类型、谁交来的、要我做什么', async () => {
    const first = (await open()).find('.task')
    expect(first.text()).toContain('退回补证')
    expect(first.text()).toContain('chen')
    expect(first.text()).toContain('缺公司正式回应')
    expect(first.text()).toContain('待办事项：上级退回')
    expect(first.text()).toContain('上一步结论：证据不足')
  })

  it('自己认领的说「自己认领」，而不是空着', async () => {
    const tasks = (await open()).findAll('.task')
    expect(tasks[1].text()).toContain('自己认领')
  })

  it('按钮随任务类型变，都直达工作台的人工研判', async () => {
    const links = (await open()).findAll('.task .go')
    expect(links.map((link) => link.text())).toEqual(['补充核验 →', '进入研判 →'])
    expect(links[0].attributes('href')).toBe('/risks/a1#decide')
  })

  it('类型切换只列有任务的那几类', async () => {
    const wrapper = await open()
    const chips = wrapper.findAll('.chip').map((chip) => chip.text())
    expect(chips).toEqual(['全部 2', '退回补证 1', '待研判 1'])
    await wrapper.findAll('.chip')[1].trigger('click')
    expect(wrapper.findAll('.task')).toHaveLength(1)
  })

  it('没有任务时指回还没人认领的公共队列', async () => {
    state.body = { ...state.body, items: [], counts: {} }
    const wrapper = await open()
    expect(wrapper.text()).toContain('暂无分配给您的任务')
    expect(wrapper.find('.empty a').attributes('href')).toBe('/risks?status=pending')
    expect(wrapper.text()).toContain('12 条待研判事项尚未认领')
  })
})
