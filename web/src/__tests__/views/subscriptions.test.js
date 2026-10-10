/** 我的预警订阅（修改思路 v2 §九）：第一屏只有业务字段，技术配置只有管理员能填。 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = { role: 'analyst', created: [] }
const LISTING = {
  items: [{ subscription_id: 's1', name: '长鑫高风险', created_by: 'wei', company_id: 'cxmt',
            issue_code: null, min_risk_level: 'high', reason_codes: ['claim_conflict'],
            quiet_hours: null, channel: 'webhook', target: 'http://127.0.0.1:8787/hook',
            secret_ref: 'NOTIFY_SECRET', dry_run: true, enabled: true }],
  reasons: { claim_conflict: '存在与权威披露直接冲突的主张', issue_escalating: '所属专题处于升温状态' },
  allowlist: ['http://127.0.0.1:*'],
}

vi.mock('../../api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  api: {
    notifySubscriptions: vi.fn(async () => LISTING),
    notifyCreate: vi.fn(async (body) => { state.created.push(body); return body }),
    notifyToggle: vi.fn(async () => ({})),
  },
}))
vi.mock('../../session.js', async (importOriginal) => ({
  ...(await importOriginal()),
  allows: (required) => ['viewer', 'analyst', 'reviewer', 'admin'].indexOf(state.role)
    >= ['viewer', 'analyst', 'reviewer', 'admin'].indexOf(required),
}))

import SubscriptionsView from '../../views/SubscriptionsView.vue'

async function open() {
  const wrapper = mount(SubscriptionsView, { global: { stubs: {
    CompanySelect: true, RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } } })
  for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
  return wrapper
}

beforeEach(() => {
  state.role = 'analyst'
  state.created = []
})

describe('我的预警订阅', () => {
  it('业务用户看不到推送地址与密钥名，技术配置里的输入框是灰的', async () => {
    const wrapper = await open()
    expect(wrapper.text()).toContain('我的预警订阅')
    expect(wrapper.text()).not.toContain('http://127.0.0.1:8787/hook')
    expect(wrapper.text()).not.toContain('签名密钥：NOTIFY_SECRET')
    for (const input of wrapper.findAll('.tech input')) {
      expect(input.attributes('disabled')).toBeDefined()
    }
    expect(wrapper.text()).toContain('由管理员配置')
  })

  it('管理员仍看得见并能填技术配置', async () => {
    state.role = 'admin'
    const wrapper = await open()
    expect(wrapper.text()).toContain('http://127.0.0.1:8787/hook')
    expect(wrapper.text()).toContain('签名密钥：NOTIFY_SECRET')
  })

  it('提醒条件用业务的话列出，勾上的随订阅一起送出', async () => {
    const wrapper = await open()
    expect(wrapper.text()).toContain('仅当：存在与权威披露直接冲突的主张')
    wrapper.vm.draft.name = '冲突提醒'
    await wrapper.find('.reasons input[type="checkbox"]').setValue(true)
    await wrapper.find('button.create').trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(state.created[0]).toMatchObject({ name: '冲突提醒', channel: 'inapp',
                                            reason_codes: ['claim_conflict'] })
    expect(state.created[0]).not.toHaveProperty('target')
  })
})
