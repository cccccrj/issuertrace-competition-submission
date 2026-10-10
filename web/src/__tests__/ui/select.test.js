/** 自绘下拉，以及它欠原生 `<select>` 的那笔债。
 *
 *  换掉原生下拉的理由是外观：弹出的列表由操作系统绘制，CSS 够不着，在这套界面
 *  里它是唯一一块来自别处的东西。但原生 `<select>` 白送键盘操作、读屏支持和
 *  首字母跳转，**换成 div 拼的控件，这些默认全部消失，只剩好看**。
 *
 *  所以这份测试全部是那笔债：每一条都对应原生控件本来就会做、而自绘控件必须
 *  自己补上的一件事。少任何一条，这次替换就是拿可访问性换观感。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import Select from '../../components/ui/Select.vue'

const OPTIONS = [
  { value: '', label: '全部' },
  { value: 'high', label: '高风险', count: 136 },
  { value: 'medium', label: '中风险', count: 196 },
  { value: 'low', label: '低风险' },
]

function render(props = {}) {
  return mount(Select, {
    props: { options: OPTIONS, label: '风险等级', modelValue: '', ...props },
    attachTo: document.body,
  })
}

const key = (wrapper, k) => wrapper.find('button.control').trigger('keydown', { key: k })
const active = (wrapper) => wrapper.find('[data-active="true"]').text()

beforeEach(() => { document.body.innerHTML = '' })

describe('展开与收起', () => {
  it('默认收起，选项不在 DOM 里', () => {
    const wrapper = render()
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })

  it('点按钮展开，再点收起', async () => {
    const wrapper = render()
    await wrapper.find('button.control').trigger('click')
    expect(wrapper.find('[role="listbox"]').exists()).toBe(true)
    await wrapper.find('button.control').trigger('click')
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })

  it('Enter、空格、上下箭头都能展开', async () => {
    for (const k of ['Enter', ' ', 'ArrowDown', 'ArrowUp']) {
      const wrapper = render()
      await key(wrapper, k)
      expect(wrapper.find('[role="listbox"]').exists(), k).toBe(true)
    }
  })

  it('Escape 收起', async () => {
    const wrapper = render()
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'Escape')
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })

  it('Tab 收起，且不拦截焦点', async () => {
    // 拦下 Tab 会把用户困在控件里，这是自绘下拉最常见的坏法。
    // 派发真事件而不是用 trigger：`trigger` 不把事件还回来，
    // 「事件存在」这种断言等于什么都没查。
    const wrapper = render()
    await key(wrapper, 'ArrowDown')
    const event = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    wrapper.find('button.control').element.dispatchEvent(event)
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
    expect(event.defaultPrevented).toBe(false)
  })

  it('方向键与 Enter 会被拦截，避免连带滚页面或提交表单', async () => {
    // 与 Tab 相反的一面：这几个键必须拦，否则 ↓ 会滚动页面、
    // Enter 会把外层表单提交掉。
    const wrapper = render()
    await key(wrapper, 'ArrowDown')
    for (const k of ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter']) {
      const fresh = render()
      await key(fresh, 'ArrowDown')
      const event = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })
      fresh.find('button.control').element.dispatchEvent(event)
      expect(event.defaultPrevented, k).toBe(true)
    }
  })

  it('点控件外面收起', async () => {
    const wrapper = render()
    await wrapper.find('button.control').trigger('click')
    document.body.click()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })
})

describe('键盘选择', () => {
  it('展开时高亮停在当前值上，不是回到第一项', async () => {
    // 26 项的议题码表里，每次从头开始意味着要重新走一遍才回到原处。
    const wrapper = render({ modelValue: 'medium' })
    await key(wrapper, 'ArrowDown')
    expect(active(wrapper)).toContain('中风险')
  })

  it('上下箭头移动高亮并在两端回绕', async () => {
    const wrapper = render()
    await key(wrapper, 'ArrowDown')      // 展开，高亮「全部」
    await key(wrapper, 'ArrowDown')
    expect(active(wrapper)).toContain('高风险')
    await key(wrapper, 'ArrowUp')
    await key(wrapper, 'ArrowUp')
    expect(active(wrapper)).toContain('低风险')
  })

  it('Home 与 End 跳到首尾', async () => {
    const wrapper = render()
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'End')
    expect(active(wrapper)).toContain('低风险')
    await key(wrapper, 'Home')
    expect(active(wrapper)).toContain('全部')
  })

  it('Enter 选中高亮项并收起', async () => {
    const wrapper = render()
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'Enter')
    expect(wrapper.emitted('update:modelValue')[0]).toEqual(['high'])
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })

  it('键入字符按标签跳转', async () => {
    // 原生下拉自带首字母跳转，去掉就是净损失。
    const wrapper = render()
    await key(wrapper, 'ArrowDown')
    await key(wrapper, '低')
    expect(active(wrapper)).toContain('低风险')
  })

  it('高亮移动本身不改值', async () => {
    // 移动到某一项不等于选了它；只有 Enter 或点击才是决定。
    const wrapper = render()
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'ArrowDown')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })
})

describe('读屏能拿到的东西', () => {
  it('按钮报出自己是下拉、是否展开、以及它的名字', async () => {
    const wrapper = render()
    const control = wrapper.find('button.control')
    expect(control.attributes('aria-haspopup')).toBe('listbox')
    expect(control.attributes('aria-expanded')).toBe('false')
    expect(control.attributes('aria-label')).toBe('风险等级')
    await control.trigger('click')
    expect(wrapper.find('button.control').attributes('aria-expanded')).toBe('true')
  })

  it('高亮项通过 aria-activedescendant 报出，焦点不离开按钮', async () => {
    // 焦点跑进列表项是另一种常见坏法：Esc 之后就不知道该还给谁了。
    const wrapper = render()
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'ArrowDown')
    const described = wrapper.find('button.control').attributes('aria-activedescendant')
    expect(described).toBeTruthy()
    expect(document.getElementById(described).textContent).toContain('高风险')
  })

  it('选中由 aria-selected 承担，不是由高亮承担', async () => {
    // 两件事挤进同一种视觉，读屏和肉眼会看到不同的答案。
    const wrapper = render({ modelValue: 'low' })
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'Home')
    const selected = wrapper.findAll('[aria-selected="true"]')
    expect(selected).toHaveLength(1)
    expect(selected[0].text()).toContain('低风险')
    expect(active(wrapper)).toContain('全部')
  })

  it('列表自己也带名字', async () => {
    const wrapper = render()
    await wrapper.find('button.control').trigger('click')
    expect(wrapper.find('[role="listbox"]').attributes('aria-label')).toBe('风险等级')
  })

  it('禁用时不展开', async () => {
    const wrapper = render({ disabled: true })
    await wrapper.find('button.control').trigger('click')
    await key(wrapper, 'ArrowDown')
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })
})

describe('显示', () => {
  it('按钮上显示当前值的标签', () => {
    expect(render({ modelValue: 'high' }).find('.value').text()).toBe('高风险')
  })

  it('计数在选项里，选之前就看得到', async () => {
    const wrapper = render()
    await wrapper.find('button.control').trigger('click')
    expect(wrapper.text()).toContain('136')
  })

  it('取不到当前值时退回第一项，而不是空白', async () => {
    // 一个显示空白的筛选器读起来像「没有筛」，而它其实筛着。
    expect(render({ modelValue: 'gone' }).find('.value').text()).toBe('全部')
  })
})

describe('禁用的选项', () => {
  const WITH_DISABLED = [
    { value: 'a', label: '甲' },
    { value: 'b', label: '乙', disabled: true },
    { value: 'c', label: '丙' },
  ]
  const withDisabled = (props = {}) => mount(Select, {
    props: { options: WITH_DISABLED, label: '状态', modelValue: 'a', ...props },
    attachTo: document.body,
  })

  it('照常列出，但点不动', async () => {
    // 只列不给选，和干脆不列是两件事：前者说明「有这个选项，但你不能选」。
    const wrapper = withDisabled()
    await wrapper.find('button.control').trigger('click')
    await wrapper.findAll('[role="option"]')[1].trigger('click')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(wrapper.find('[role="listbox"]').exists()).toBe(true)
  })

  it('方向键跳过它', async () => {
    const wrapper = withDisabled()
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'ArrowDown')
    expect(active(wrapper)).toContain('丙')
  })

  it('读屏也知道它不能选', async () => {
    const wrapper = withDisabled()
    await wrapper.find('button.control').trigger('click')
    expect(wrapper.findAll('[role="option"]')[1].attributes('aria-disabled')).toBe('true')
  })

  it('End 落在最后一个可选项上，而不是最后一项', async () => {
    const wrapper = mount(Select, {
      props: { label: '状态', modelValue: 'a',
               options: [...WITH_DISABLED, { value: 'd', label: '丁', disabled: true }] },
      attachTo: document.body,
    })
    await key(wrapper, 'ArrowDown')
    await key(wrapper, 'End')
    expect(active(wrapper)).toContain('丙')
  })
})

describe('Escape 不只在按钮有焦点时管用', () => {
  it('从 document 派发的 Escape 也关掉浮层', async () => {
    // 点开之后焦点可能已经被别处拿走；按钮上的 keydown 覆盖不到这一条。
    const wrapper = render()
    await wrapper.find('button.control').trigger('click')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })
})
