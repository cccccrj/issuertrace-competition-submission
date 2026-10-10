/** The wrapper, checked for the promises it makes on every chart's behalf.
 *
 *  ECharts itself is mocked. What is under test is not that a library draws —
 *  it is that adopting a library did not cost the properties this interface had
 *  before it: a sentence describing every figure, the numbers available as
 *  text, and a chart that says so when it has nothing to show.
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const instance = {
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
}

vi.mock('../../charts/echarts.js', () => ({
  default: { init: vi.fn(() => instance) },
}))

import echarts from '../../charts/echarts.js'
import EChart from '../../charts/EChart.vue'

const builder = (tokens) => ({ series: [{ type: 'bar', data: [1], itemStyle: { color: tokens['series-1'] } }] })

const base = {
  builder,
  label: '按月份的预警数量，高 3 条、中 5 条',
  tableColumns: ['月份', '数量'],
  tableRows: [['2026-01', 3], ['2026-02', 5]],
}

/** jsdom reports every element as 0x0, which is exactly the collapsed-container
 *  case the component guards against; give it a width when we want it to mount. */
function withWidth(width) {
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true, get: () => width,
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  withWidth(600)
  window.ResizeObserver = class {
    constructor(callback) { this.callback = callback; instance.__observer = this }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
})

describe('无障碍承诺', () => {
  it('图形容器带 role="img" 与调用方给的那句话', () => {
    const wrapper = mount(EChart, { props: base })
    const figure = wrapper.find('[role="img"]')
    expect(figure.exists()).toBe(true)
    expect(figure.attributes('aria-label')).toBe(base.label)
  })

  it('表格替代始终渲染，不是可选项也不是懒加载', () => {
    const wrapper = mount(EChart, { props: base })
    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(wrapper.find('thead').text()).toContain('月份')
    expect(rows[0].text()).toContain('2026-01')
  })

  it('提供纹理开关，供无法区分色相时使用', async () => {
    const wrapper = mount(EChart, { props: base })
    await wrapper.find('button.decal').trigger('click')
    const option = instance.setOption.mock.calls.at(-1)[0]
    expect(option.aria.decal.show).toBe(true)
  })

  it('没有数据时用一句话说明，而不是画一张空网格', () => {
    const wrapper = mount(EChart, {
      props: { ...base, tableRows: [], empty: '该公司在本窗口内没有预警。' },
    })
    expect(wrapper.text()).toContain('该公司在本窗口内没有预警。')
    expect(wrapper.find('[role="img"]').isVisible()).toBe(false)
  })
})

describe('生命周期', () => {
  it('容器宽度为 0 时不初始化——折叠行、隐藏标签页与 jsdom 都是这种状态', () => {
    withWidth(0)
    mount(EChart, { props: base })
    expect(echarts.init).not.toHaveBeenCalled()
  })

  it('容器拿到宽度后由 resize 回调补上初始化', async () => {
    withWidth(0)
    const wrapper = mount(EChart, { props: base })
    expect(echarts.init).not.toHaveBeenCalled()
    withWidth(600)
    instance.__observer.callback()
    expect(echarts.init).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('已初始化后 resize 只是重排，不重复 init', () => {
    mount(EChart, { props: base })
    instance.__observer.callback()
    expect(echarts.init).toHaveBeenCalledTimes(1)
    expect(instance.resize).toHaveBeenCalled()
  })

  it('卸载时释放实例与观察器', () => {
    const wrapper = mount(EChart, { props: base })
    wrapper.unmount()
    expect(instance.dispose).toHaveBeenCalled()
  })

  it('用 notMerge 重设选项，避免上一主题的残留混进来', () => {
    mount(EChart, { props: base })
    expect(instance.setOption.mock.calls[0][1]).toEqual({ notMerge: true })
  })
})

describe('构造器契约', () => {
  it('构造器拿到的是令牌，页面因此不必自己取色', () => {
    const spy = vi.fn(() => ({ series: [] }))
    mount(EChart, { props: { ...base, builder: spy } })
    const tokens = spy.mock.calls[0][0]
    expect(tokens).toHaveProperty('series-1')
    expect(tokens).toHaveProperty('status-critical')
  })
})
