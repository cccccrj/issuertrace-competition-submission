/** 登录页 — the first screen, and the page the refactor forgot.
 *
 *  Phases F, G and H named list pages, detail pages and the shell. The door was
 *  on none of those lists, so it went untouched from the commit that first added
 *  the platform and kept describing a system that no longer exists. It also had
 *  no test at all, which is why nothing said so.
 *
 *  These pin the three things that were actually wrong, in the form that fails
 *  if they come back.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = { health: { status: 'ok', events: 1995, model_gateway: null } }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, api: { health: vi.fn(async () => state.health) } }
})

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, useRouter: () => ({ replace: vi.fn(), currentRoute: { value: { query: {} } } }) }
})

import LoginView from '../../views/LoginView.vue'

async function render() {
  const wrapper = mount(LoginView)
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  state.health = { status: 'ok', events: 1995, model_gateway: null }
})

describe('第一屏说的话必须还是真的', () => {
  it('不再自称「离线」——那个说法核对命题后已被撤销', async () => {
    // 命题原文全文没有「离线」二字。It was this project's own risk-register
    // wording, over-extended into a platform-wide claim, and it sat in the
    // opening sentence of the first screen long after being retracted.
    expect((await render()).text()).not.toContain('离线')
  })

  it('不再自称「仅做只读展示」——复核、推送与处置都会写入', async () => {
    // Three write paths exist. The claim that survives is the enforced one:
    // the analysis layer is opened mode=ro and human records live in their own
    // tables.
    const text = (await render()).text()
    expect(text).not.toContain('仅做只读展示')
    expect(text).toContain('不改写处理层结论')
  })

  it('六项任务都在门外就说清楚了', async () => {
    // The sidebar behind the door is grouped by exactly these six; the cover
    // used to advertise three capabilities from before half of them existed.
    const text = (await render()).text()
    for (const task of ['多源事件识别', '要素抽取与标准化', '可信度评估与辨伪',
                        '同源聚合与去重', '事件脉络追踪', '影响分级与预警推送']) {
      expect(text, task).toContain(task)
    }
  })
})

describe('这是账号密码登录，不是一句共享口令', () => {
  it('两个字段叫「账号」和「密码」', async () => {
    // It always was username + password; the labels said 「口令」, which reads
    // like one shared passphrase for everybody and made a reader ask why there
    // were no accounts.
    const text = (await render()).text()
    expect(text).toContain('账号')
    expect(text).toContain('密码')
    expect(text).not.toContain('口令')
  })

  it('门上不列账号，也不谈密码从哪来', async () => {
    // A test-account list belongs in the deployment note, not on the door: the
    // first screen is not the place to explain the platform's key management,
    // and a page that lists who may enter reads like a lobby directory.
    const text = (await render()).text()
    expect(text).not.toContain('测试账号')
    expect(text).not.toContain('部署方')
    expect(text).not.toContain('analyst')
    expect(text).not.toContain('reviewer')
  })
})

describe('能力飘带', () => {
  it('复制出来的那一份对读屏隐藏——否则六项能力会被念两遍', async () => {
    // The seamless loop needs the cards rendered twice. A screen reader must
    // still meet six of them, not twelve.
    const wrapper = await render()
    const sets = wrapper.findAll('.ribbon-set')
    expect(sets).toHaveLength(2)
    expect(sets[0].attributes('aria-hidden')).toBeUndefined()
    expect(sets[1].attributes('aria-hidden')).toBe('true')
  })

  it('暴露给读屏的正好六张卡', async () => {
    const wrapper = await render()
    const spoken = wrapper.findAll('.ribbon-set:not([aria-hidden]) .ribbon-card')
    expect(spoken).toHaveLength(6)
  })

  it('两份内容逐字相同——接缝错位是这种飘带最典型的坏法', async () => {
    // And the one nobody catches by looking: the join is off-screen for most of
    // a 40-second cycle, so a mismatched duplicate reads as a glitch that comes
    // and goes.
    const wrapper = await render()
    const [first, second] = wrapper.findAll('.ribbon-set')
      .map((set) => set.findAll('.ribbon-card').map((card) => card.text()))
    expect(second).toEqual(first)
  })

  it('每一个动画都进了 prefers-reduced-motion 守卫', () => {
    // These are the only animations in the entire product, so nothing else
    // would notice if one slipped out of the guard — and adding a third and
    // forgetting it is exactly how that happens. Reading the source is the
    // cheapest way to hold a media query jsdom cannot evaluate.
    const source = readFileSync(
      join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'views', 'LoginView.vue'), 'utf8')
    const guard = source.match(/@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\n\}/)
    expect(guard, '没有 prefers-reduced-motion 守卫').not.toBeNull()

    const declared = [...source.matchAll(/animation:\s*([\w-]+)\s/g)].map((m) => m[1])
    expect(declared.length).toBeGreaterThan(0)
    for (const name of new Set(declared)) {
      // Every named animation must be switched off somewhere inside the guard.
      expect(guard[0], `${name} 没有被 reduced-motion 关掉`).toMatch(/animation:\s*none/)
    }
    expect(guard[0]).toContain('.ribbon-track')
    expect(guard[0]).toContain('.ribbon-card::after')
  })
})

describe('封面的图标与日历', () => {
  it('六张卡各有一个图标，两份一样，且对读屏隐藏——名字已经写在卡上', async () => {
    const wrapper = await render()
    const [first, second] = wrapper.findAll('.ribbon-set')
    const shapes = (set) => set.findAll('.ribbon-icon').map((icon) => icon.html())
    expect(shapes(first)).toHaveLength(6)
    expect(new Set(shapes(first)).size).toBe(6)          // 六个图标互不相同
    expect(shapes(second)).toEqual(shapes(first))        // 接缝处看不出换了一份
    for (const icon of first.findAll('.ribbon-icon')) expect(icon.attributes('aria-hidden')).toBe('true')
  })

  it('系数画成竖条，最高的那一处单独标出', async () => {
    const phases = (await render()).findAll('.phase')
    // 高度写成 rem 跟着界面缩放走；换回 1 倍下的像素再比
    const heights = phases.map((phase) => parseFloat(phase.find('.phase-bar').attributes('style').match(/height:\s*([\d.]+)rem/)[1]) * 16)
    expect(Math.max(...heights)).toBe(28)
    expect(Math.min(...heights)).toBe(8)
    const peaks = phases.filter((phase) => phase.classes().includes('peak'))
    expect(peaks.map((phase) => phase.find('.phase-label').text())).toEqual(['发行询价'])
  })
})

describe('封面只留名词', () => {
  it('六个能力不带序号，也不带解释', async () => {
    // Trimmed twice: the note under each, then the 一二三四五六 ordinals. Both
    // were the door explaining what is behind it before anyone opened it.
    const text = (await render()).text()
    expect(text).toContain('可信度评估与辨伪')
    expect(text).not.toMatch(/[一二三四五六]\s*多源事件识别/)
    expect(text).not.toContain('守门决定纳入与排除')
  })
})

describe('页面上唯一的数字是取来的，不是写死的', () => {
  it('事件数来自 /api/health', async () => {
    state.health = { status: 'ok', events: 4242, model_gateway: null }
    expect((await render()).text()).toContain('4,242')
  })

  it('取不到时不显示一个编造的数，那半句消失而页面照常', async () => {
    // A number typed into a login screen is a number nobody updates, and a
    // placeholder is worse than an absence.
    state.health = Promise.reject(new Error('down'))
    const wrapper = mount(LoginView)
    await new Promise((resolve) => setTimeout(resolve, 0))
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).not.toMatch(/[\d,]+\s*起事件/)
    expect(wrapper.find('.submit').exists()).toBe(true)
  })

  it('公司名也是取来的：此前封面上写死两家，加第三家公司时登录页永远只说两家', async () => {
    state.health = { status: 'ok', events: 12, model_gateway: null,
                     companies: [{ company_id: 'x1', name: '甲公司' }, { company_id: 'x2', name: '乙公司' }] }
    const text = (await render()).text()
    expect(text).toContain('甲公司 · 乙公司')
  })
})
