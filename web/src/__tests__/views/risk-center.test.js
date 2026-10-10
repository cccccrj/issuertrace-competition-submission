/** 风险预警中心（修改思路 v2 §三）：四个数都点得进去，点之前的数就是点之后的那张表。 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const SUMMARY = {
  total: 3,
  indicators: { pending: 2, high_open: 1, anomaly: 2, mine_pending: 1, my_tasks: 1 },
  anomalies: [
    { key: 'conflict', label: '多源证据冲突', detail: '主张与披露矛盾', action: '查看冲突事项', count: 1 },
    { key: 'thin_source', label: '仅转述或简讯来源', detail: '首发为简讯', action: '查找首发', count: 1 },
  ],
  companies: [],
}
const ROW = {
  alert_id: 'r1', company_id: 'cxmt', headline: '长鑫科技｜募资', risk_level: 'high',
  adjusted_risk_score: 70, credibility: 0.6, reasons: [], latest_progress: '2026-09-20',
  disposition: { status: 'pending', status_label: '待研判', owner: null },
}

vi.mock('../../api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  api: {
    riskSummary: vi.fn(async () => SUMMARY),
    risks: vi.fn(async () => ({ total: 1, items: [ROW] })),
    overviewTrend: vi.fn(async () => ({ months: [], series: [] })),
  },
}))

import RiskCenterView from '../../views/RiskCenterView.vue'

const STUBS = {
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
  EChart: true, CompanySelect: true,
}
async function open() {
  const wrapper = mount(RiskCenterView, { global: { stubs: STUBS } })
  for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
  return wrapper
}

beforeEach(() => {
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

describe('风险预警中心', () => {
  it('四张卡回答「今天要处理什么」，待研判旁边说出几条分派给了我', async () => {
    const text = (await open()).text()
    for (const label of ['待研判', '高风险未结', '异常待核查', '待我处理']) expect(text).toContain(label)
    expect(text).toContain('其中 1 条已分派给你')
  })

  it('每类异常点进去只看那一类，并写明下一步做什么', async () => {
    const links = (await open()).findAll('a.anomaly')
    expect(links.map((link) => link.attributes('href')))
      .toEqual(['/risks/anomalies?anomaly=conflict', '/risks/anomalies?anomaly=thin_source'])
    expect(links[1].text()).toContain('查找首发')
  })

  it('队列的按钮叫「进入研判」，并且看得出有没有人认领', async () => {
    const wrapper = await open()
    const go = wrapper.findAll('a').find((link) => link.attributes('href') === '/risks/r1')
    expect(go.text()).toContain('进入研判')
    expect(wrapper.text()).toContain('未认领')
    expect(wrapper.text()).not.toContain('研判档案')
  })
})
