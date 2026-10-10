/** The shared layer, checked for the properties the duplication used to bury.
 *
 *  Each of these components replaces between three and sixteen hand-written
 *  copies. What matters is not that they render — it is that the behaviours the
 *  copies got subtly wrong are now decided once, here, and asserted.
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { COMPANY_LABELS } from '../../api.js'
import AsyncSection from '../../components/ui/AsyncSection.vue'
import Callout from '../../components/ui/Callout.vue'
import CompanySelect from '../../components/ui/CompanySelect.vue'
import DataTable from '../../components/ui/DataTable.vue'
import EnumSelect from '../../components/ui/EnumSelect.vue'
import FilterBar from '../../components/ui/FilterBar.vue'
import PageHeader from '../../components/ui/PageHeader.vue'

const COLUMNS = [
  { key: 'name', title: '名称' },
  { key: 'count', title: '数量', align: 'right' },
]
const ROWS = [{ id: 'a', name: '甲', count: 2 }, { id: 'b', name: '乙', count: 5 }]

/** 下拉现在是自绘的 listbox，选项只在展开后存在——原生 `<select>` 的
 *  `<option>` 始终在 DOM 里，这两条测试因此要先把它打开。选中的值仍然是
 *  组件 emit 出来的那个，这才是这几条真正钉住的东西。 */
async function optionsOf(wrapper) {
  await wrapper.find('button.control').trigger('click')
  return wrapper.findAll('[role="option"]')
}

describe('CompanySelect', () => {
  it('公司来自 api.js 的标签表，不是页面里硬编码的选项', async () => {
    // Seven views hard-coded these. Reading the map is what makes a third
    // company a config change instead of a seven-file edit.
    const wrapper = mount(CompanySelect)
    const options = await optionsOf(wrapper)
    expect(options).toHaveLength(1 + Object.keys(COMPANY_LABELS).length)
    expect(wrapper.text()).toContain(Object.values(COMPANY_LABELS)[0])
  })

  it('「全部」选出来的值是空串——api.js 的 request 会丢弃空参数', async () => {
    // 从选项文本改成断言 emit：空串在 wire 上代表「不筛」，而那是 emit 的值，
    // 不是某个 DOM 属性。
    const wrapper = mount(CompanySelect, { props: { modelValue: 'cxmt' } })
    const options = await optionsOf(wrapper)
    await options[0].trigger('click')
    expect(wrapper.emitted('update:modelValue')[0]).toEqual([''])
  })
})

describe('EnumSelect', () => {
  it('把码原样传出去，不做大小写归一', async () => {
    // The casing differences between endpoints are deliberate; normalising here
    // would silently break the ones that expect upper case.
    const wrapper = mount(EnumSelect, { props: { labels: { HIGH: '高', low: '低' } } })
    const options = await optionsOf(wrapper)
    await options[1].trigger('click')
    expect(wrapper.emitted('update:modelValue')[0]).toEqual(['HIGH'])
  })

  it('facets 数组带上计数，读者选之前就知道有多少', async () => {
    const wrapper = mount(EnumSelect, {
      props: { options: [{ value: 'high', label: '高', count: 136 }] },
    })
    await optionsOf(wrapper)
    expect(wrapper.text()).toContain('高')
    expect(wrapper.text()).toContain('136')
  })
})

describe('AsyncSection', () => {
  it('空态用调用方那句话，没有通用默认值', () => {
    const wrapper = mount(AsyncSection, {
      props: { empty: true, emptyText: '该事件没有可定位的原子主张，已排除在核验成功率分母之外。' },
      slots: { default: '<p>内容</p>' },
    })
    expect(wrapper.text()).toContain('已排除在核验成功率分母之外')
    expect(wrapper.text()).not.toContain('内容')
  })

  it('加载中不显示内容，也不显示空态', () => {
    const wrapper = mount(AsyncSection, {
      props: { loading: true, emptyText: 'x' }, slots: { default: '<p>内容</p>' },
    })
    expect(wrapper.text()).toContain('加载中')
    expect(wrapper.text()).not.toContain('内容')
  })

  it('出错时是一条 alert，不是静默的空表', () => {
    const wrapper = mount(AsyncSection, { props: { error: '取数失败', emptyText: 'x' } })
    expect(wrapper.find('[role="alert"]').text()).toBe('取数失败')
  })
})

describe('Callout', () => {
  it('颜色之外总有文字，色相不单独承载含义', () => {
    const wrapper = mount(Callout, { props: { tone: 'bad', title: '注意' }, slots: { default: '语料缺失' } })
    expect(wrapper.text()).toContain('注意')
    expect(wrapper.text()).toContain('语料缺失')
  })
})

describe('FilterBar', () => {
  it('有筛选生效时给出清除入口并报出条数', () => {
    // Without it a reader who set a filter two pages ago reads a partial table
    // as if it were the whole.
    const wrapper = mount(FilterBar, { props: { active: 3 } })
    expect(wrapper.find('button.reset').text()).toContain('3')
  })

  it('没有筛选时不显示清除', () => {
    expect(mount(FilterBar, { props: { active: 0 } }).find('button.reset').exists()).toBe(false)
  })
})

describe('PageHeader', () => {
  it('操作放在 actions 插槽里，不再用 float 脱离文档流', () => {
    const wrapper = mount(PageHeader, {
      props: { title: '事件台账', note: '说明' },
      slots: { actions: '<a href="#">导出报告</a>' },
      global: { stubs: { RouterLink: true } },
    })
    expect(wrapper.find('.actions').text()).toBe('导出报告')
    expect(wrapper.find('.page-title').text()).toBe('事件台账')
  })
})

describe('DataTable', () => {
  const mountTable = (props = {}, slots = {}) =>
    mount(DataTable, { props: { columns: COLUMNS, rows: ROWS, rowKey: 'id', ...props }, slots })

  it('每行一条数据，表头齐全', () => {
    const wrapper = mountTable()
    expect(wrapper.findAll('tbody tr')).toHaveLength(2)
    expect(wrapper.find('thead').text()).toContain('名称')
  })

  it('可展开时给出指示符——今天可点的行和不可点的行长得一样', () => {
    const wrapper = mountTable({ expandable: true })
    expect(wrapper.find('tbody .chevron').text()).toBe('▸')
    expect(wrapper.find('tbody tr').attributes('aria-expanded')).toBe('false')
  })

  it('点击展开，再点收起，同时只开一行', async () => {
    const wrapper = mountTable({ expandable: true })
    await wrapper.findAll('tbody tr')[0].trigger('click')
    expect(wrapper.emitted('update:expanded').at(-1)).toEqual(['a'])
    await wrapper.setProps({ expanded: 'a' })
    await wrapper.findAll('tbody tr')[0].trigger('click')
    expect(wrapper.emitted('update:expanded').at(-1)).toEqual([null])
  })

  it('展开行用 colspan 铺满，插槽拿到那一行', async () => {
    const wrapper = mountTable(
      { expandable: true, expanded: 'a' },
      { expand: '<template #expand="{ row }"><b>{{ row.name }}</b></template>' },
    )
    const detail = wrapper.find('tr.detail td')
    expect(detail.attributes('colspan')).toBe('3')
    expect(detail.text()).toBe('甲')
  })

  it('键盘也能展开，不只是鼠标', async () => {
    const wrapper = mountTable({ expandable: true })
    await wrapper.findAll('tbody tr')[0].trigger('keydown.enter')
    expect(wrapper.emitted('update:expanded').at(-1)).toEqual(['a'])
  })

  it('不可展开时不发 update:expanded，也不加 tabindex', async () => {
    const wrapper = mountTable()
    await wrapper.findAll('tbody tr')[0].trigger('click')
    expect(wrapper.emitted('update:expanded')).toBeUndefined()
    expect(wrapper.find('tbody tr').attributes('tabindex')).toBeUndefined()
  })

  it('列插槽负责渲染徽标之类的单元格', () => {
    const wrapper = mount(DataTable, {
      props: { columns: [{ key: 'name', title: '名称', slot: 'name' }], rows: ROWS, rowKey: 'id' },
      slots: { name: '<template #name="{ row }"><i>{{ row.name }}</i></template>' },
    })
    expect(wrapper.find('tbody i').text()).toBe('甲')
  })
})
