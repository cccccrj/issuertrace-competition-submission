/** I-02：核查卡片上的数值核对。换算由后端精确算出，这里只钉住两件事——
 *  读者看得到正确的换算与差值；模型结论与数值核对相左时，有一条醒目的提示。 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import NumericCheck from '../../components/NumericCheck.vue'

const I02 = {
  status: 'agree',
  disputed: true,
  dispute: 'figures_agree',
  unmatched: [],
  figures: [{
    status: 'agree', claim_raw: '708.15亿股', evidence_raw: '7,081,505.7468 万股',
    claim_value: '70,815,000,000股', evidence_value: '70,815,057,468股',
    difference: '57,468股', relative: '0.000081%',
  }],
}

describe('NumericCheck', () => {
  it('shows the exact conversion and difference', () => {
    const text = mount(NumericCheck, { props: { check: I02 } }).text()
    expect(text).toContain('70,815,000,000股')
    expect(text).toContain('70,815,057,468股')
    expect(text).toContain('差 57,468股')
    expect(text).toContain('在主张的表述精度内一致')
  })

  it('flags a model verdict the figures contradict', () => {
    const wrapper = mount(NumericCheck, { props: { check: I02 } })
    expect(wrapper.find('[role="note"]').text()).toContain('数值核对与系统研判结论不符')
  })

  it('words a unit slip on a supported verdict differently', () => {
    const check = { ...I02, status: 'slip', dispute: 'unit_slip' }
    expect(mount(NumericCheck, { props: { check } }).find('[role="note"]').text()).toContain('系统研判判为「支持」')
  })

  it('stays quiet when the model and the figures agree', () => {
    const wrapper = mount(NumericCheck, { props: { check: { ...I02, disputed: false } } })
    expect(wrapper.find('[role="note"]').exists()).toBe(false)
  })

  it('renders nothing without a check', () => {
    expect(mount(NumericCheck, { props: { check: null } }).html()).not.toContain('数值核对')
  })
})
