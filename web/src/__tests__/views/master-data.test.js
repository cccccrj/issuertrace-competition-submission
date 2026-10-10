/** 词表页 —— 全站唯一自称解释词表的页面。
 *
 *  它此前只有「码 / 名称 / 使用次数」：一个想弄清「技术水平和业务拓展怎么分」的读者
 *  被导航引到这里，得到的是「TECH_LEVEL / 技术水平 / 912」。定义一直就在处理层里
 *  （pipeline.CODE_GUIDE、EVENT_TYPE_GUIDE、MODE_GUIDE），一个字都没到过界面。
 *
 *  这几条钉两件事：每个词条都有定义；而定义只出现在折叠块或 title 里，不作段落常显
 *  —— 2026-09-06 刚因「解释性文字太多」净删过 2,592 字，新增的解释不能把它加回去。
 *  （2026-09-07 每页加回了一句 40 字以内的导语，那是「这一页在回答什么问题」；
 *  词表的定义与判据仍然只能折叠或 title，这一条没变。）
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const VOCABULARIES = {
  event_type: {
    title: '事件类型',
    source: 'event_records.event_types_json',
    judging_rules: '- 十一个实质类目按下列顺序取第一个命中的……这是顺序上的最后一类，不是省事的一类。',
    groups: null,
    terms: [
      { code: 'IPO_REFINANCING', label: '发行与再融资', used: 344,
        definition: '发行与再融资：证券发行本身及其审核程序……判据是**正在发行或审核一笔证券**。',
        group: null },
      { code: 'FINANCIAL_AUDIT', label: '财务与审计', used: 552,
        definition: '财务与审计：已经形成的经营成果与财务状况本身……', group: null },
    ],
    undeclared: [],
  },
  impact_object: {
    title: '影响对象',
    source: 'event_records.affected_objects_json',
    judging_rules: '- 只给这件事实际改变了、或直接指向的对象……',
    groups: [{ group: '公司内部', codes: ['operations'] }, { group: '外溢', codes: ['regional_economy'] }],
    terms: [
      { code: 'operations', label: '经营运作', used: 12, definition: '经营与业务：日常经营活动本身……',
        group: '公司内部' },
      { code: 'regional_economy', label: '区域经济', used: 0,
        definition: '区域经济：项目或事件所在地的地方经济、就业与财政。', group: '外溢' },
    ],
    undeclared: [],
  },
}

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      vocabularies: vi.fn(async () => VOCABULARIES),
      companies: vi.fn(async () => ({ companies: [], last_reviewed: null })),
      organizations: vi.fn(async () => ({ organizations: [] })),
      financials: vi.fn(async () => ({ items: [] })),
    },
  }
})

import MasterDataView from '../../views/MasterDataView.vue'

async function render() {
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const wrapper = mount(MasterDataView, {
    global: { stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
  })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})

describe('词表页的判据', () => {
  it('每个词条的定义都到得了界面', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('判据是**正在发行或审核一笔证券**')
    expect(wrapper.text()).toContain('区域经济：项目或事件所在地的地方经济')
  })

  it('判定规则跟着词表一起给出——它是「什么情况下不要用这个类」那一半', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('不是省事的一类')
  })

  it('定义只在折叠块或 title 里，不作段落常显', async () => {
    const wrapper = await render()
    const collapsed = wrapper.findAll('details')
    expect(collapsed.length).toBeGreaterThan(0)
    for (const block of collapsed) {
      expect(block.attributes('open')).toBeUndefined()
    }
    // 名称列悬停即得定义。
    const titled = wrapper.findAll('[title]').map((node) => node.attributes('title'))
    expect(titled.some((text) => text?.includes('证券发行本身'))).toBe(true)
  })

  it('影响对象带着它的五组分组示人——分组本身是判据', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('外溢')
    expect(wrapper.text()).toContain('公司内部')
  })

  it('口径与限制写明官方金标覆盖不到哪五类', async () => {
    // 12 类里有 5 类金标支持为 0，而其中一类是本平台语料最大的一类。
    const wrapper = await render()
    expect(wrapper.text()).toContain('无从测量')
  })
})
