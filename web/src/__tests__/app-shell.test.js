/** The shell, checked for the thing a config provider could quietly break.
 *
 *  The library is wrapped round the whole tree with `abstract`, which is meant
 *  to render no element of its own.  If it ever renders one, the flex layout
 *  gains a wrapper between `.layout` and its children and the sidebar stops
 *  being a sidebar — a failure that looks like a styling accident and is
 *  actually a structural one.  These assert the structure rather than trusting
 *  the flag, and they are the acceptance test for "the interface is unchanged".
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createRouter, createWebHistory } from 'vue-router'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api.js', () => ({
  api: {
    notifications: vi.fn(async () => ({ unread: 0, items: [] })),
    markNotificationsRead: vi.fn(async () => ({})),
    health: vi.fn(async () => ({ status: 'ok', companies: [] })),
    systemStatus: vi.fn(async () => ({ analysis_service: { ok: true, checked_at: null }, needs_upgrade: false })),
  },
  loadCompanyLabels: vi.fn(async () => ({})),
  companyLabel: (id) => id || '—',
  formatDate: (value) => String(value || '—').slice(0, 10),
}))

import { session } from '../session.js'
import { ENTRY_POINTS, routes as realRoutes } from '../router.js'
import App from '../App.vue'

const Blank = { template: '<div class="page">页面</div>' }

/** 真路由表，只把组件换成空白页。
 *
 *  这里曾经手抄过一份 22 条路径的 CHROMED 数组——于是「每条路由都有导航入口」
 *  这条断言读的是副本，而副本正是漏掉新页面的那一份。现在装的是 router.js 导出
 *  的同一个数组：漏挂 `meta.nav` 会当场变红，而不是等到有人发现某一页点不到。 */
function routes() {
  return createRouter({
    history: createWebHistory(),
    // 重定向没有组件，也不该有导航入口——它是一条旧地址，不是一个页面。
    routes: realRoutes.map((route) => (route.redirect ? route : { ...route, component: Blank })),
  })
}

const router = routes

async function shell(path = '/') {
  const instance = router()
  instance.push(path)
  await instance.isReady()
  const wrapper = mount(App, { global: { plugins: [instance] } })
  await Promise.resolve()
  wrapper.router = instance
  return wrapper
}

/** 组标题的文字，去掉右侧的 ▸/▾。 */
const titlesOf = (wrapper) => wrapper.findAll('.group-title').map((node) => node.find('span').text())
/** 当前展开的组。以 aria-expanded 为准——它同时是读屏读到的那个状态。 */
const expandedOf = (wrapper) => wrapper.findAll('.group-title')
  .filter((node) => node.attributes('aria-expanded') === 'true')
  .map((node) => node.find('span').text())

beforeEach(() => {
  session.ready = true
  session.authenticated = true
  session.username = 'reviewer'
  session.role = 'reviewer'
  localStorage.clear()
})

describe('app shell', () => {
  it('侧栏是 .layout 的直接子元素，中间没有多出包装节点', async () => {
    const wrapper = await shell('/')
    const layout = wrapper.find('.layout')
    expect(layout.exists()).toBe(true)
    const children = Array.from(layout.element.children).map((node) => node.className)
    expect(children).toContain('sidebar')
    expect(children.some((name) => String(name).includes('main'))).toBe(true)
  })

  it('每条有外壳的路由都有入口：要么在侧栏，要么在它声明的那个位置', async () => {
    // Pinned to the router rather than to a count, so a page can never ship
    // without a way to reach it — which is how the two push pages would have
    // arrived if this asserted "12" forever.
    //
    // 有几页挪出了侧栏（通知箱进铃铛、帮助与订阅进账号块……），这条不变式换了写法
    // 而没有作废：`meta.entry` 必须是 ENTRY_POINTS 里的一个键，且那个入口真的渲染出来。
    const wrapper = await shell('/')
    // 通知箱的入口在铃铛展开的抽屉里：先打开它，再收集外壳上的全部链接。
    await wrapper.find('.bell').trigger('click')
    await new Promise((resolve) => setTimeout(resolve))
    const hrefs = wrapper.findAll('.nav-link').map((link) => link.attributes('href'))
    const anywhere = wrapper.findAll('a').map((link) => link.attributes('href'))
    const chromed = routes()
      .getRoutes()
      .filter((route) => !route.meta.bare && !route.meta.public && !route.path.includes(':')
                         && !route.redirect)
    expect(new Set(hrefs).size).toBe(hrefs.length)   // no duplicates
    for (const route of chromed) {
      const { nav, entry } = route.meta
      expect(Boolean(nav) !== Boolean(entry), `${route.path} 要么 meta.nav、要么 meta.entry，恰好一个`).toBe(true)
      if (nav) {
        expect(hrefs, `${route.path} 没有导航入口`).toContain(route.path)
        continue
      }
      expect(ENTRY_POINTS, `${route.path} 的 meta.entry 不是已知入口`).toHaveProperty(entry)
      if (entry === 'search') {
        expect(wrapper.find('.sidebar input[aria-label="全局检索"]').exists()).toBe(true)
      } else if (entry === 'tab') {
        // 事项库页内的视图切换由 views/risks.test.js 钉住；这里只保证它不在侧栏里重复出现。
        expect(hrefs).not.toContain(route.path)
      } else {
        expect(anywhere, `${route.path} 声明入口在「${ENTRY_POINTS[entry]}」，但外壳上没有`).toContain(route.path)
      }
    }
  })

  it('认不出的地址回首页，而不是留一个只有外壳的空白页', async () => {
    // 新设备从 /index.html 或旧书签进来，登录后被 ?redirect= 原样带回——曾经是空白正文。
    for (const path of ['/index.html', '/dashboard', '/alerts/old-id']) {
      const wrapper = await shell(path)
      expect(wrapper.router.currentRoute.value.path, path).toBe('/')
    }
    // 登录页按 ?redirect= 跳转时走的是 replace，同样落到首页。
    const wrapper = await shell('/todo')
    await wrapper.router.replace('/index.html')
    expect(wrapper.router.currentRoute.value.path).toBe('/')
  })

  it('认得的地址不受兜底影响', async () => {
    for (const path of ['/risks', '/risks/anomalies', '/events/abc', '/report/event/abc']) {
      const wrapper = await shell(path)
      expect(wrapper.router.currentRoute.value.path, path).toBe(path)
    }
  })

  it('导航对所有角色可见——隐藏用不了的控件是礼貌，服务端仍会拒绝', async () => {
    const wrapper = await shell('/')
    const labels = wrapper.findAll('.nav-link').map((link) => link.text())
    // 第一条就是默认首页：进来先看「今天该处理哪几条」。
    expect(labels[0]).toContain('风险预警中心')
    expect(labels[1]).toContain('我的任务')
    for (const wanted of ['风险事项库', '证据核验明细', '上报与推送记录', '标注复核', '账号权限']) {
      expect(labels.some((text) => text.includes(wanted)), wanted).toBe(true)
    }
    // 挪出侧栏的几页不再以链接形式出现在导航里。
    for (const gone of ['使用指南', '全局检索', '通知箱', '预警订阅', '核验异常', '已归档']) {
      expect(labels.some((text) => text.includes(gone)), gone).toBe(false)
    }
  })

  it('分组按「今天要完成什么」排，后两组是证明与后台', async () => {
    // 侧栏此前逐字写着命题的六项主要任务——「读侧栏即读任务表」。那个设计对评委
    // 成立，对每天用它的人不成立。命题六项任务 → 平台页面的对照表在 /guide，
    // 由 views/guide.test.js 逐字钉住。改这里之前先去看那一条。
    const wrapper = await shell('/')
    expect(titlesOf(wrapper)).toEqual([
      '工作台', '风险事项', '公司与专题', '调查与处置', '数据与质量', '系统管理',
    ])
  })

  it('无外壳路由不渲染侧栏', async () => {
    const wrapper = await shell('/login')
    expect(wrapper.find('.sidebar').exists()).toBe(false)
    expect(wrapper.find('.layout').exists()).toBe(false)
  })

  it('没有侧栏的页面也能切主题', async () => {
    // The control lived in the sidebar footer, so the login page and the
    // printable report — both of which have a light and a dark face — had no
    // way to change it. It is fixed to the viewport now and does not hang off
    // the chrome flag.
    for (const path of ['/login', '/']) {
      const wrapper = await shell(path)
      expect(wrapper.find('.sidebar').exists(), path).toBe(path === '/')
      expect(wrapper.find('.theme button.control').exists(), `${path} 没有主题切换`).toBe(true)
    }
  })

  it('有侧栏时主题控件在正文右上角的工具条里，占一行而不浮动', async () => {
    // 走过两个位置：浮在右上角（压住页面自己的「导出风险简报」），再是侧栏底部（不压东西，
    // 但没人去那里找）。工具条是 .main 里的一行，把页标题往下推，而不是盖住它。
    const chromed = await shell('/')
    expect(chromed.find('.main > .topbar .theme button.control').exists()).toBe(true)
    expect(chromed.find('.main > .topbar .bell').exists(), '通知铃铛与主题并排').toBe(true)
    expect(chromed.find('.sidebar .theme').exists(), '侧栏里不该再有第二个主题控件').toBe(false)
    expect(chromed.find('.theme.floating').exists(), '有侧栏时不该还有浮动按钮').toBe(false)

    const bare = await shell('/login')
    expect(bare.find('.theme.floating button.control').exists()).toBe(true)
  })

  it('三套主题一次摊开，选中即写回 localStorage 与 data-theme', async () => {
    // It used to be a cycling button: you had to click three times to learn how
    // many themes there were, and could not go straight to the one you wanted.
    const wrapper = await shell('/')
    for (const value of ['dark', 'sepia', 'light']) {
      await wrapper.find('.theme button.control').trigger('click')
      const options = wrapper.findAll('.theme [role="option"]')
      expect(options.map((o) => o.text().replace('✓', ''))).toEqual(['浅色', '深色', '护眼'])
      await options[['light', 'dark', 'sepia'].indexOf(value)].trigger('click')
      expect(localStorage.getItem('theme-choice')).toBe(value)
      expect(document.documentElement.getAttribute('data-theme')).toBe(value)
      // Choosing closes the menu; leaving it open behind the click is the most
      // common way a hand-rolled dropdown misbehaves.
      expect(wrapper.findAll('.theme [role="option"]')).toHaveLength(0)
    }
  })

  it('Escape 关掉菜单', async () => {
    const wrapper = await shell('/')
    await wrapper.find('.theme button.control').trigger('click')
    expect(wrapper.findAll('.theme [role="option"]').length).toBe(3)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll('.theme [role="option"]')).toHaveLength(0)
  })

  it('认不出的存储值落到默认的深色，而不是写出一个没人认识的 data-theme', async () => {
    // 「跟随系统」 is gone but a reader's localStorage may still hold it. Writing
    // it through would set data-theme="auto", which matches none of the three
    // token blocks — the page would silently fall back to the unset look.
    localStorage.setItem('theme-choice', 'auto')
    const wrapper = await shell('/')
    expect(wrapper.find('.theme button.control').text()).toContain('深色')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })

  it('没选过主题的人看到深色，且打开页面本身不替他记下任何选择', async () => {
    const wrapper = await shell('/')
    expect(wrapper.find('.theme button.control').text()).toContain('深色')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(localStorage.getItem('theme-choice')).toBeNull()
  })

  it('旧键里自动写下的浅色不再算数，亲手选过的才记住', async () => {
    // 旧代码每次打开页面都写 'theme'，分不出是选的还是默认值。
    localStorage.setItem('theme', 'light')
    await shell('/')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    localStorage.setItem('theme-choice', 'sepia')
    await shell('/')
    expect(document.documentElement.getAttribute('data-theme')).toBe('sepia')
  })

  it('index.html 的预设与 App.vue 用同一个键、同一个默认值', () => {
    const read = (relative) => readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', relative), 'utf8')
    const html = read('../index.html')
    const app = read('App.vue')
    expect(app).toMatch(/const THEME_KEY = 'theme-choice'/)
    expect(app).toMatch(/const DEFAULT_THEME = 'dark'/)
    expect(html).toContain("localStorage.getItem('theme-choice')")
    expect(html).toMatch(/: 'dark'\)/)
  })

  it('账号与角色仍显示在侧栏底部', async () => {
    const wrapper = await shell('/')
    const foot = wrapper.find('.foot')
    expect(foot.text()).toContain('reviewer')
    expect(foot.text()).toContain('核查负责人')
    expect(foot.text()).toContain('退出')
  })
})

describe('侧栏抬头', () => {
  it('平台名完整，且不再自称「离线」', async () => {
    // Two separate rots in one four-word block: the name had lost its last two
    // characters, and the strapline under it still read 「命题二 · 离线数据平台」
    // — the retracted framing, surviving here after being cleaned out of the
    // login page and every document.
    const wrapper = await shell('/')
    const brand = wrapper.find('.brand')
    expect(brand.text()).toBe('上市公司事件智能平台')
    expect(wrapper.text()).not.toContain('离线')
    expect(wrapper.text()).not.toContain('命题二')
  })
})

describe('侧栏底部的账号块不压在导航上', () => {
  // jsdom does no layout, so the overlap itself is invisible to a test. Its
  // cause is not: `.foot` was `position: absolute; bottom: 16px` inside the
  // sidebar, and the sidebar scrolls. Absolute positioning resolves against the
  // padding box, so once seven nav groups grew taller than the viewport the
  // block stopped sitting at the bottom of anything — it sat 100vh down the
  // *content*, on top of whichever links happened to be there (传播链路 and
  // 议题台账, as it turned out).
  const read = (relative) => readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', relative), 'utf8')

  it('账号块不是绝对定位', () => {
    const foot = read('App.vue').match(/\.foot\s*\{[^}]*\}/)
    expect(foot, '.foot 规则不见了').not.toBeNull()
    expect(foot[0], '.foot 又变回绝对定位了——它在可滚动的侧栏里会漂到列表中间')
      .not.toMatch(/position:\s*absolute/)
    expect(foot[0]).toMatch(/margin-top:\s*auto/)
  })

  it('侧栏是纵向 flex，底部块才有地方待', () => {
    const sidebar = read('styles.css').match(/\.sidebar\s*\{[^}]*\}/)
    expect(sidebar[0]).toMatch(/flex-direction:\s*column/)
  })
})

/** 侧栏说得出自己是什么。
 *
 *  七个分组照抄命题的任务名，而 GuideView 的注释早就承认过：「对第一次来的人是
 *  二十一个陌生名词」。大白话是**加在旁边**，不是替换掉——两种读者要的是同一份
 *  导航的两个读法，所以这里同时钉住「有大白话」和「任务名一个字没变」。
 */
describe('侧栏说人话', () => {
  it('品牌下有一行说清整个平台是干嘛的', async () => {
    // 这里曾经有过一行副标题，被删是因为内容错了（写着已撤销的说法），
    // 不是因为副标题这个形式不好。
    const wrapper = await shell('/')
    const strap = wrapper.find('.brand-strap')
    expect(strap.exists(), '品牌下没有副标题').toBe(true)
    expect(strap.text().length).toBeGreaterThan(6)
  })

  it('每个分组各有一句大白话，只在展开时显示', async () => {
    const wrapper = await shell('/')
    const hints = wrapper.findAll('.group-hint')
    expect(hints).toHaveLength(6)
    for (const hint of hints) expect(hint.text().length).toBeGreaterThan(4)
    // 收起的组连同它的注解一起收起；组标题的 title 仍带着这句话，悬停可见。
    const shown = wrapper.findAll('.group-links').filter((node) => node.element.style.display !== 'none')
    expect(shown).toHaveLength(1)
    expect(wrapper.find('.group-title').attributes('title').length).toBeGreaterThan(4)
  })
})

/** 手风琴。修改思路 v2 §二-A：一次只展开一组，展开哪组由当前地址决定。 */
describe('侧栏手风琴', () => {
  it('首页只展开工作台', async () => {
    const wrapper = await shell('/')
    expect(expandedOf(wrapper)).toEqual(['工作台'])
    expect(wrapper.find('.group.open .chevron').text()).toBe('▾')
    expect(wrapper.findAll('.group:not(.open) .chevron').map((node) => node.text()))
      .toEqual(['▸', '▸', '▸', '▸', '▸'])
  })

  it.each([
    ['/risks', '风险事项'],
    ['/risks/abc123', '风险事项'],     // 详情页落在它列表页所在的组
    ['/risks/archived', '风险事项'],
    ['/events/42', '风险事项'],
    ['/ops/quality', '数据与质量'],
    ['/users', '系统管理'],
  ])('进入 %s 时展开「%s」', async (path, group) => {
    const wrapper = await shell(path)
    expect(expandedOf(wrapper)).toEqual([group])
  })

  it('不在侧栏里的页面（公司档案、通知箱）落到工作台，而不是全部收起', async () => {
    for (const path of ['/companies/cxmt', '/notifications']) {
      const wrapper = await shell(path)
      expect(expandedOf(wrapper), path).toEqual(['工作台'])
    }
  })

  it('点标题只切换展开，不跳转；展开一组时其余收起', async () => {
    const wrapper = await shell('/')
    const titles = () => wrapper.findAll('.group-title')
    await titles()[4].trigger('click')
    expect(expandedOf(wrapper)).toEqual(['数据与质量'])
    expect(wrapper.router.currentRoute.value.path).toBe('/')
    await titles()[4].trigger('click')
    expect(expandedOf(wrapper)).toEqual([])
  })

  it('换页后展开的是新页面所在的组', async () => {
    const wrapper = await shell('/')
    await wrapper.router.push('/investigations')
    await wrapper.vm.$nextTick()
    expect(expandedOf(wrapper)).toEqual(['调查与处置'])
  })

  it('标题是真按钮，aria-controls 指向它收放的那一块', async () => {
    const wrapper = await shell('/')
    for (const title of wrapper.findAll('.group-title')) {
      expect(title.element.tagName).toBe('BUTTON')
      expect(wrapper.find(`#${title.attributes('aria-controls')}`).exists()).toBe(true)
    }
  })
})

describe('铃铛与账号块', () => {
  it('未读数挂在右上角的铃铛上', async () => {
    const { api } = await import('../api.js')
    api.notifications.mockResolvedValueOnce({ unread: 7, items: [] })
    const wrapper = await shell('/')
    await new Promise((resolve) => setTimeout(resolve))
    const bell = wrapper.find('.topbar button.bell')
    expect(bell.find('.badge').text()).toBe('7')
    expect(bell.attributes('aria-label')).toContain('7 条未读')
  })

  it('铃铛展开一个抽屉，点一条直达那条事项，而不是先进通知页', async () => {
    const { api } = await import('../api.js')
    const wrapper = await shell('/')
    api.notifications.mockResolvedValueOnce({ unread: 1, items: [
      { notification_id: 'n1', kind: 'handoff', company_id: 'cxmt', created_at: '2026-09-23',
        title: '退回给你补证：长鑫科技｜募资', body: 'chen：缺出处', link: '/risks/a1', read_at: null },
    ] })
    const bell = wrapper.find('.bell')
    expect(bell.attributes('aria-expanded')).toBe('false')
    await bell.trigger('click')
    await new Promise((resolve) => setTimeout(resolve))
    expect(bell.attributes('aria-expanded')).toBe('true')
    const notice = wrapper.find('#notice-drawer a.notice')
    expect(notice.attributes('href')).toBe('/risks/a1')
    expect(notice.text()).toContain('交接给我')
    expect(notice.text()).toContain('缺出处')
    await notice.trigger('click')
    expect(api.markNotificationsRead).toHaveBeenCalledWith(['n1'])
    const links = wrapper.findAll('#notice-drawer .drawer-foot a').map((link) => link.attributes('href'))
    expect(links).toEqual([])      // 点完抽屉就收起
  })

  it('Escape 也能收起抽屉', async () => {
    const wrapper = await shell('/')
    await wrapper.find('.bell').trigger('click')
    await new Promise((resolve) => setTimeout(resolve))
    expect(wrapper.find('#notice-drawer').exists()).toBe(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('#notice-drawer').exists()).toBe(false)
  })

  it('使用帮助与订阅设置在账号块里', async () => {
    const wrapper = await shell('/')
    const links = wrapper.findAll('.foot .foot-link').map((link) => link.attributes('href'))
    expect(links).toEqual(['/guide', '/subscriptions'])
  })
})

/** 窄屏侧栏收成抽屉。
 *
 *  jsdom 不认媒体查询，「400px 时侧栏不挤正文」本身测不到；测得到的是两头：
 *  开关的状态与可访问名（菜单按钮 ↔ 侧栏），以及样式表里那条断点确实把侧栏
 *  收起、且收起时用的是 visibility——链接要留在 DOM 里，又不能留在 Tab 顺序里。
 */
describe('窄屏侧栏抽屉', () => {
  const read = (relative) => readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', relative), 'utf8')

  it('菜单按钮在工具条里，aria-controls 指向侧栏', async () => {
    const wrapper = await shell('/')
    const menu = wrapper.find('.topbar .menu')
    expect(menu.exists()).toBe(true)
    expect(menu.attributes('aria-label')).toBe('导航菜单')
    expect(menu.attributes('aria-controls')).toBe('site-nav')
    expect(wrapper.find('.sidebar').attributes('id')).toBe('site-nav')
  })

  it('点菜单开合侧栏，遮罩只在打开时出现，点遮罩收起', async () => {
    const wrapper = await shell('/')
    const menu = wrapper.find('.menu')
    expect(menu.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.sidebar').attributes('data-open')).toBeUndefined()
    expect(wrapper.find('.nav-scrim').exists()).toBe(false)

    await menu.trigger('click')
    expect(menu.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('.sidebar').attributes('data-open')).toBeDefined()

    await wrapper.find('.nav-scrim').trigger('click')
    expect(menu.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.nav-scrim').exists()).toBe(false)
  })

  it('Escape 收起抽屉，焦点回到菜单按钮', async () => {
    const wrapper = await shell('/')
    document.body.appendChild(wrapper.element)
    await wrapper.find('.menu').trigger('click')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.menu').attributes('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(wrapper.find('.menu').element)
    wrapper.unmount()
  })

  it('换页后抽屉收起', async () => {
    const wrapper = await shell('/')
    await wrapper.find('.menu').trigger('click')
    await wrapper.router.push('/alerts')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.menu').attributes('aria-expanded')).toBe('false')
  })

  it('断点里侧栏固定在左侧、收起时 visibility:hidden；宽屏规则不变', () => {
    const css = read('styles.css')
    const block = css.match(/@media \(max-width: 760px\) \{\s*\.sidebar \{[^}]*\}\s*\.sidebar\[data-open\] \{[^}]*\}/)
    expect(block, '窄屏断点不见了').not.toBeNull()
    expect(block[0]).toMatch(/position:\s*fixed/)
    expect(block[0]).toMatch(/visibility:\s*hidden/)
    // 菜单按钮宽屏下藏起，断点内才显示——两处断点必须是同一个值。
    const app = read('App.vue')
    expect(app).toMatch(/\.menu \{ display: none;/)
    expect(app).toMatch(/@media \(max-width: 760px\) \{\s*\.menu \{ display: inline-flex; \}/)
  })
})
