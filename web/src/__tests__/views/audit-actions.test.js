/** 审计页第七类：平台操作。
 *
 *  登录、导出、权限变更都不改变任何业务数据，没有别的表会顺带记下它们——
 *  `platform_actions` 是七类里唯一一张**为了留痕而建**的表。这里钉两条：
 *
 *  * 动作名与结果名跟着接口一起下来，这一页不自己抄一份（抄一份就是给同一个量
 *    起第二个名字，而这一页恰恰是用来对口径的）；
 *  * 空表要说自己是空的，并且说清「不回填」——补一段看似完整的历史比承认缺记录更坏。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const PAYLOAD = {
  total: 2,
  by_action: [{ action: 'login', rows: 1 }, { action: 'role_change', rows: 1 }],
  actions: { login: '登录', role_change: '变更角色', report_export: '导出报告' },
  outcomes: { ok: '成功', refused: '被拒绝', failed: '失败' },
  items: [
    { action_seq: 2, occurred_at: '2026-09-22T10:00:00+00:00', actor: 'boss', role: 'admin',
      action: 'role_change', object_kind: 'account', object_kind_label: '账号', object_label: 'analyst', outcome: 'ok',
      client: '10.0.0.8', detail: '分析师 → 核查负责人' },
    { action_seq: 1, occurred_at: '2026-09-22T09:00:00+00:00', actor: 'wei', role: 'analyst',
      action: 'login', object_kind: null, object_label: null, outcome: 'refused',
      client: '10.0.0.9', detail: '账号或密码不正确' },
  ],
}

const state = { payload: null }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      auditRuns: vi.fn(async () => ({ items: [], total: 0 })),
      auditActions: vi.fn(async () => state.payload),
      auditDecisions: vi.fn(async () => ({ items: [], total: 0 })),
      auditEvents: vi.fn(async () => ({ items: [], outcomes: [], scanned: 0, truncated: false })),
      notifyDispositionLog: vi.fn(async () => ({ items: [], total: 0 })),
      notifyOutbox: vi.fn(async () => ({ items: [], total: 0 })),
    },
  }
})

vi.mock('../../session.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    session: { ready: true, authenticated: true, username: 'boss', role: 'admin' },
  }
})

import AuditView from '../../views/AuditView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }

async function openActions() {
  const wrapper = mount(AuditView, { global: { stubs: STUBS } })
  const settle = async () => {
    for (let i = 0; i < 4; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
      await wrapper.vm.$nextTick()
    }
  }
  await settle()
  const tab = wrapper.findAll('button').find((button) => button.text() === '平台操作')
  expect(tab, '审计页要有「平台操作」这一类').toBeTruthy()
  await tab.trigger('click')
  await settle()
  return wrapper
}

beforeEach(() => {
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  state.payload = JSON.parse(JSON.stringify(PAYLOAD))
})

describe('审计页 · 平台操作', () => {
  it('七类，不是六类', async () => {
    const wrapper = await openActions()
    expect(wrapper.text()).toContain('七类留痕')
  })

  it('动作名与结果名用接口给的那一份，页面不自己抄', async () => {
    const wrapper = await openActions()
    const text = wrapper.text()
    expect(text).toContain('变更角色')
    expect(text).toContain('被拒绝')
    // 裸码不许印给读者。
    expect(text).not.toContain('role_change')
    expect(text).not.toContain('refused')
  })

  it('一行要答得出谁、对谁、从哪里、改成什么', async () => {
    const wrapper = await openActions()
    const text = wrapper.text()
    expect(text).toContain('boss')
    expect(text).toContain('analyst')
    expect(text).toContain('10.0.0.8')
    expect(text).toContain('分析师 → 核查负责人')
  })

  it('时间要精确到分，否则同一天的几行分不出先后', async () => {
    // 这一页的全部意义是「谁在什么时候做了什么」。`formatDate` 截到日，三行登录
    // 读起来一模一样——既分不开也排不出先后。
    const wrapper = await openActions()
    expect(wrapper.text()).toContain('2026-09-22 10:00')
    expect(wrapper.text()).toContain('2026-09-22 09:00')
  })

  it('被拒绝的那一行要看得出来，不与成功的混在一起', async () => {
    const wrapper = await openActions()
    expect(wrapper.find('.refused').exists()).toBe(true)
  })

  it('空表说自己是空的，并且说清不回填', async () => {
    state.payload = { total: 0, by_action: [], items: [],
                      actions: PAYLOAD.actions, outcomes: PAYLOAD.outcomes }
    const wrapper = await openActions()
    expect(wrapper.text()).toContain('不作回填')
  })
})
