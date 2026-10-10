/** 账号权限页的密码框（外部测试 I-07）。
 *
 *  此前「改我的密码」两个框只有 placeholder：读屏读出两个未命名文本框，一输入
 *  placeholder 又消失。这里钉三件事：
 *
 *  * 每个密码框都有持久的字段名，并且和控件程序化关联（按标签能找到它）；
 *  * 「至少 8 位」是挂在输入框上的说明，不是 placeholder；
 *  * 提交失败的错误就近以 alert 播报，并被输入框的 aria-describedby 引用。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = { role: 'admin', ownFails: null }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      users: vi.fn(async () => ({
        roles: [{ value: 'viewer', label: '只读' }, { value: 'admin', label: '管理员' }],
        items: [{ username: 'wei', role: 'viewer', active: true, last_login_at: null, created_by: 'boss' }],
      })),
      changeOwnPassword: vi.fn(async () => {
        if (state.ownFails) throw new Error(state.ownFails)
        return { changed: true }
      }),
      userResetPassword: vi.fn(async () => ({ changed: true })),
      userCreate: vi.fn(async () => ({})),
    },
  }
})

vi.mock('../../session.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    session: { ready: true, authenticated: true, username: 'boss', role: 'admin' },
    allows: (role) => role !== 'admin' || state.role === 'admin',
  }
})

import { api } from '../../api.js'
import UsersView from '../../views/UsersView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } }
const open = () => mount(UsersView, { global: { stubs: STUBS } })
const settle = async () => { for (let i = 0; i < 4; i += 1) await Promise.resolve() }

/** 按可见字段名找输入框：标签包住控件，等价于读屏取到的可访问名。 */
function byLabel(wrapper, text) {
  const label = wrapper.findAll('label').find((node) =>
    (node.classes('field-label') ? node : node.find('.field-label')).text() === text)
  expect(label, `找不到字段名「${text}」`).toBeTruthy()
  const target = label.attributes('for')
  return target ? wrapper.find(`#${target}`) : label.find('input')
}

describe('账号权限页的密码框', () => {
  beforeEach(() => {
    state.role = 'admin'
    state.ownFails = null
    vi.clearAllMocks()
  })

  it('当前密码与新密码各有字段名，不靠 placeholder', async () => {
    const wrapper = open()
    await settle()
    const current = byLabel(wrapper, '当前密码')
    const fresh = byLabel(wrapper, '新密码')
    expect(current.attributes('autocomplete')).toBe('current-password')
    expect(fresh.attributes('autocomplete')).toBe('new-password')
    for (const input of wrapper.findAll('input[type="password"]')) {
      expect(input.attributes('placeholder')).toBeUndefined()
    }
  })

  it('「至少 8 位」是挂在新密码框上的说明', async () => {
    const wrapper = open()
    await settle()
    const fresh = byLabel(wrapper, '新密码')
    const hint = wrapper.find(`#${fresh.attributes('aria-describedby')}`)
    expect(hint.text()).toBe('至少 8 位')
    // 说明不得并进字段名：它在 <label> 之外。
    expect(hint.element.closest('label')).toBeNull()
    expect(fresh.attributes('minlength')).toBe('8')
  })

  it('提交失败：错误就近以 alert 播报，并被输入框引用', async () => {
    state.ownFails = '当前密码不对'
    const wrapper = open()
    await settle()
    await byLabel(wrapper, '当前密码').setValue('wrong-one')
    await byLabel(wrapper, '新密码').setValue('new-password-1')
    await wrapper.find('form').trigger('submit')
    await settle()

    const alert = wrapper.find('[role="alert"]')
    expect(alert.text()).toBe('当前密码不对')
    const fresh = byLabel(wrapper, '新密码')
    expect(fresh.attributes('aria-describedby').split(' ')).toContain(alert.attributes('id'))
    expect(fresh.attributes('aria-invalid')).toBe('true')
    // 失败时不清空，用户改一个字就能重交。
    expect(fresh.element.value).toBe('new-password-1')
  })

  it('重设他人密码不再用浏览器 prompt，而是带字段名的密码框', async () => {
    const prompt = vi.spyOn(window, 'prompt')
    const wrapper = open()
    await settle()
    const button = wrapper.findAll('button').find((node) => node.text() === '重设密码')
    await button.trigger('click')
    expect(prompt).not.toHaveBeenCalled()

    const input = byLabel(wrapper, 'wei 的新密码')
    expect(input.attributes('type')).toBe('password')
    await input.setValue('reset-password-1')
    await input.element.form.dispatchEvent(new Event('submit'))
    await settle()
    expect(api.userResetPassword).toHaveBeenCalledWith('wei', 'reset-password-1')
  })
})
