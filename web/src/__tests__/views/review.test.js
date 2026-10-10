/** The review console's keyboard contract, pinned before the file is edited.
 *
 *  This page is the most stateful in the application and the least forgiving to
 *  change: the keyboard handler reaches into child components through
 *  `defineExpose`, scrolls by querying a class name in the document, and
 *  deliberately does *not* refetch after a judgement because a refetch reorders
 *  the queue and loses the reviewer's place. None of that is visible from the
 *  template, so all of it is asserted here first.
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const TASKS = {
  total: 3,
  items: [
    { task_id: 't1', kind: 'verdict', status: 'open', company_id: 'cxmt', origin: 'stratified',
      target: { text: '主张一' }, decisions: [] },
    { task_id: 't2', kind: 'verdict', status: 'open', company_id: 'cxmt', origin: 'stratified',
      target: { text: '主张二' }, decisions: [] },
  ],
}

const METRICS = {
  tasks: 300,
  queue: { open: 300 },
  by_kind: {
    verdict: {
      correct: 8, wrong: 2, unsure: 1, judged: 11, accuracy: 0.8, sample_size: 10,
      macro_f1: 0.62, labelled: 2, wrong_without_label: 0,
      accuracy_interval: { low: 0.4912, high: 0.9433, half_width: 0.2261 },
      per_class: { SUPPORTED: { precision: 0.9, recall: 0.8, f1: 0.85, support: 5, predicted: 4 } },
      confusion: { SUPPORTED: { SUPPORTED: 4, PARTIAL: 1 }, PARTIAL: { PARTIAL: 3 } },
      evidence_location: null,
    },
  },
}

const calls = { tasks: 0, judge: 0, assign: [], scopes: [] }

/** 分派相关的返回值，逐个用例改写。默认「我有自己那一份」——这是分派过之后的
 *  常态，也是队列不再撞车的那个状态。 */
const state = { mine: 2, reviewers: [{ username: 'zhang', role: 'analyst', role_label: '分析师' },
                                     { username: 'li', role: 'analyst', role_label: '分析师' }] }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      reviewTasks: vi.fn(async (params) => {
        calls.tasks += 1
        calls.scopes.push(params)
        if (params?.assignee === 'me') return { ...TASKS, total: state.mine }
        return TASKS
      }),
      reviewMetrics: vi.fn(async () => METRICS),
      reviewJudge: vi.fn(async () => { calls.judge += 1; return { ok: true } }),
      reviewSeed: vi.fn(async () => ({ created: 0 })),
      reviewApprove: vi.fn(async () => ({})),
      reviewReject: vi.fn(async () => ({})),
      reviewReviewers: vi.fn(async () => ({ items: state.reviewers })),
      reviewAssign: vi.fn(async (body) => {
        calls.assign.push(body)
        return { tasks: 6, overlap: body.overlap, assigned: { zhang: 4, li: 4 } }
      }),
    },
  }
})

vi.mock('../../session.js', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, allows: () => true,
           session: { authenticated: true, username: 'r', role: 'reviewer', ready: true } }
})

vi.mock('../../charts/echarts.js', () => ({ default: { init: vi.fn() } }))

import ReviewView from '../../views/ReviewView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }

async function render() {
  const wrapper = mount(ReviewView, { global: { stubs: STUBS }, attachTo: document.body })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

function press(key, target = document.body) {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  Object.defineProperty(event, 'target', { value: target })
  window.dispatchEvent(event)
  return event
}

beforeEach(() => {
  calls.tasks = 0
  calls.judge = 0
  calls.assign = []
  calls.scopes = []
  state.mine = 2
  state.reviewers = [{ username: 'zhang', role: 'analyst', role_label: '分析师' },
                     { username: 'li', role: 'analyst', role_label: '分析师' }]
  METRICS.by_reviewer = []
  METRICS.agreement = {}
  METRICS.double_judged = 0
  METRICS.contested = 0
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  document.body.innerHTML = ''
})

describe('键盘契约', () => {
  it('1 / 2 / 3 判定当前聚焦的任务', async () => {
    await render()
    press('1')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(calls.judge).toBe(1)
  })

  it('焦点在输入框里时按键不判定', async () => {
    // Typing a note must never submit a judgement.
    await render()
    const input = document.createElement('input')
    document.body.appendChild(input)
    press('1', input)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(calls.judge).toBe(0)
  })

  it('焦点在下拉框里时按键不判定', async () => {
    await render()
    const select = document.createElement('select')
    document.body.appendChild(select)
    press('1', select)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(calls.judge).toBe(0)
  })

  it('焦点在可编辑区域里时按键不判定', async () => {
    // The guard tested `tagName` alone, which is right only while every control
    // on the page is a native element. A contenteditable typed a `1` straight
    // into judge().
    await render()
    const box = document.createElement('div')
    box.setAttribute('contenteditable', 'true')
    document.body.appendChild(box)
    press('1', box)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(calls.judge).toBe(0)
  })

  it('焦点在下拉控件内部的元素上时也不判定', async () => {
    // A composed select renders a div and puts focus on a descendant of it.
    await render()
    const combobox = document.createElement('div')
    combobox.setAttribute('role', 'combobox')
    const inner = document.createElement('span')
    combobox.appendChild(inner)
    document.body.appendChild(combobox)
    press('1', inner)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(calls.judge).toBe(0)
  })

  it('判定之后不重新拉取列表', async () => {
    // The queue is ordered by status; refetching reorders it and loses the
    // reviewer's place. The comment in the source says so and this holds it.
    await render()
    const before = calls.tasks
    press('1')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(calls.tasks).toBe(before)
  })

  it('j / k 与方向键移动焦点', async () => {
    await render()
    const down = press('j')
    expect(down.defaultPrevented).toBe(true)
    const up = press('ArrowUp')
    expect(up.defaultPrevented).toBe(true)
  })

  it('无关按键不被拦截', async () => {
    await render()
    expect(press('x').defaultPrevented).toBe(false)
  })
})

describe('任务卡的定位契约', () => {
  it('卡片根元素带 .task 类名——滚动定位靠它', async () => {
    // ReviewView scrolls with document.querySelectorAll('.task'); renaming the
    // class silently breaks navigation without breaking anything visible.
    const wrapper = await render()
    expect(wrapper.findAll('.task').length).toBeGreaterThan(0)
  })
})

describe('复核指标', () => {
  it('准确率与 macro-F1 都显示', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('准确率')
    expect(text).toMatch(/macro-?F1/i)
  })

  it('准确率带 95% 区间，而不是一个光秃秃的百分数', async () => {
    // 判 10 条得到的 80% 和判 300 条得到的 80% 是两句不同的话，页面上曾经
    // 长得一模一样。区间同时是「判多少条才够」的停止规则。
    const wrapper = await render()
    expect(wrapper.text()).toContain('95% 区间')
    expect(wrapper.text()).toContain('49.1%')
    expect(wrapper.text()).toContain('94.3%')
  })

  it('样本量与「无法判断」的条数一同呈现', async () => {
    // An accuracy without its denominator is not a measurement, and unsure
    // answers are excluded from it — both facts belong beside the number.
    const wrapper = await render()
    expect(wrapper.text()).toContain('10')
    expect(wrapper.text()).toContain('无法判断')
  })
})

describe('分派与按账号统计', () => {
  it('分派过之后，默认只看自己那一份', async () => {
    // 队列排序是确定的：三个人同时打开，看到的是同一批前几条，于是判的也是同一
    // 批。默认落在自己那份，撞车才不会发生。
    await render()
    expect(calls.scopes.some((scope) => scope?.assignee === 'me')).toBe(true)
    expect(calls.scopes.at(-1)?.assignee).toBe('me')
  })

  it('还没分派过时不把页面筛成空的', async () => {
    // 「我的」是 0 条时仍默认自己那份，页面会看起来像坏了。
    state.mine = 0
    await render()
    expect(calls.scopes.at(-1)?.assignee).toBeFalsy()
  })

  it('分派提交的范围就是当前筛选，且只发还没判的', async () => {
    const wrapper = await render()
    wrapper.vm.dealing.assignees = ['zhang', 'li']
    wrapper.vm.dealing.overlap = 3
    await wrapper.vm.deal()
    expect(calls.assign).toHaveLength(1)
    expect(calls.assign[0]).toMatchObject({
      assignees: ['zhang', 'li'], overlap: 3, status: 'open', kind: 'verdict',
    })
  })

  it('按账号统计逐人列出，并写明那是系统的准确率不是这个人的', async () => {
    METRICS.by_reviewer = [
      { reviewer: 'zhang', kind: 'verdict', judged: 40, correct: 34, wrong: 6, unsure: 0,
        sample_size: 40, system_accuracy: 0.85, accuracy_interval: null },
      { reviewer: 'li', kind: 'verdict', judged: 40, correct: 24, wrong: 16, unsure: 0,
        sample_size: 40, system_accuracy: 0.6, accuracy_interval: null },
    ]
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('zhang')
    expect(text).toContain('85.0%')
    expect(text).toContain('60.0%')
    // 「张三的准确率 85%」会被读成在给复核者打分，而没有金标就没人能打这个分。
    expect(text).toContain('并非复核人本身的准确率')
  })

  it('κ 与原始一致率一起给，并说明 κ 扣掉了什么', async () => {
    METRICS.by_reviewer = [{ reviewer: 'zhang', kind: 'verdict', judged: 40, correct: 34,
                             wrong: 6, unsure: 0, sample_size: 40, system_accuracy: 0.85,
                             accuracy_interval: null }]
    METRICS.agreement = { verdict: [{ pair: ['li', 'zhang'], n: 40, observed: 0.85,
                                      expected: 0.61, kappa: 0.62 }] }
    const wrapper = await render()
    expect(wrapper.text()).toContain('0.62')
    expect(wrapper.text()).toContain('同判 40 条')
  })

  it('两人只用过同一种判定时，说的是「没测出东西」而不是「完全一致」', async () => {
    // κ 无分母时报 None。把它显示成 0 会读成完全不一致，正好是反面；显示成
    // 「一致率 100%」则会读成标注质量极好，同样是反面。
    METRICS.by_reviewer = [{ reviewer: 'zhang', kind: 'verdict', judged: 20, correct: 20,
                             wrong: 0, unsure: 0, sample_size: 20, system_accuracy: 1,
                             accuracy_interval: null }]
    METRICS.agreement = { verdict: [{ pair: ['li', 'zhang'], n: 20, observed: 1,
                                      expected: 1, kappa: null }] }
    const wrapper = await render()
    expect(wrapper.text()).toContain('不代表「完全一致」')
  })

  it('没有人双标过时，说明一致性无从计算而不是显示 0', async () => {
    METRICS.by_reviewer = [{ reviewer: 'zhang', kind: 'verdict', judged: 40, correct: 34,
                             wrong: 6, unsure: 0, sample_size: 40, system_accuracy: 0.85,
                             accuracy_interval: null }]
    const wrapper = await render()
    expect(wrapper.text()).toContain('无法计算一致性')
  })
})
