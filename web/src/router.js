import { createRouter, createWebHistory } from 'vue-router'

import { refreshSession, session } from './session.js'

// 每个页面一个块，按需取。此前 25 个页面静态导入打成一个 422 KB 的 chunk——登录页
// 也要先把二十几个它永远不会渲染的页面下载完；而 ECharts 那 657 KB 只有画图的页面该付。
// vue-router 原生支持异步组件，路由守卫与 meta 不受影响。
const view = (name) => () => import(`./views/${name}.vue`)

/** 侧栏分组。顺序即呈现顺序；`key` 由每条路由的 `meta.nav.group` 引用。
 *
 *  按「这个人今天要完成什么」分，而不是按「系统有什么功能」分：前四组是每天的活，
 *  后两组是证明与后台。侧栏是手风琴，一次只展开一组，所以后两组平时收着，不占
 *  业务人员的视线（修改思路 v2 §二-A）。
 *
 *  每组仍带一行 `hint`：任务名是写给懂命题的人的目录，大白话是给第一次来的人的。
 *  它只在该组展开时显示——两种读者要的是同一份导航的两个读法，但不必同时摊开六句。 */
export const NAV_GROUPS = [
  { key: 'workbench', title: '工作台', hint: '今日待办与个人任务' },
  { key: 'risks', title: '风险事项', hint: '风险事项从识别到归档的全流程' },
  { key: 'company', title: '公司与专题', hint: '公司与专题的动态追踪' },
  { key: 'handling', title: '调查与处置', hint: '核查、上报与推送' },
  { key: 'data', title: '数据与质量', hint: '数据来源、结构化结果与研判质量' },
  { key: 'system', title: '系统管理', hint: '来源词库、标注复核、留痕与账号' },
]

/** 不挂侧栏、但另有入口的页面，入口在哪。
 *
 *  「每个页面都点得到」这条不变式此前全靠 `meta.nav` 保证；有几页挪出了侧栏，
 *  这条不变式就得换一种写法，而不是悄悄作废——`meta.entry` 必须是这里的一个键，
 *  app-shell.test.js 逐条核对它在外壳或宿主页上真的存在。 */
export const ENTRY_POINTS = {
  bell: '正文右上角工具条的通知铃铛',
  help: '侧栏底部的「使用帮助」',
  account: '侧栏底部的「订阅设置」',
  search: '侧栏顶部的检索框（回车进入完整结果页）',
  tab: '风险事项库页内的视图切换',
}

/** 路由表是导航的唯一来源。
 *
 *  此前同一份清单存了三份：这里、`App.vue` 的 NAV 常量、以及 `app-shell.test.js`
 *  里的 CHROMED 数组。三份要同时改，于是「新页面没有入口」这件事只能靠一条断言
 *  拦——而那条断言自己也读的是那份副本。现在 `meta.nav` 写在路由上，侧栏是这张表
 *  的投影：漏挂一条就是漏一个入口，多挂一条不可能，两者不会再各说各话。
 *
 *  没有 `meta.nav` 的路由不出现在侧栏：详情页、登录页、报告页和重定向是一类，
 *  带 `meta.entry` 的几页是另一类——它们有入口，只是不在侧栏里。 */
export const routes = [
  { path: '/login', component: view('LoginView'), meta: { public: true, bare: true } },
  // A report is a document, so it renders without the app chrome and prints
  // as what it is rather than as a screenshot of a page.
  { path: '/report/:kind/:id', component: view('ReportView'), meta: { bare: true } },

  { path: '/guide', component: view('GuideView'), meta: { entry: 'help' } },
  // 检索框本身就在侧栏顶部；此前它下面还挂着一条同名链接，同一件事两个入口。
  { path: '/search', component: view('SearchView'), meta: { entry: 'search' } },

  { path: '/', component: view('RiskCenterView'), meta: { nav: { label: '风险预警中心', group: 'workbench', order: 0 } } },
  { path: '/todo', component: view('TasksView'), meta: { nav: { label: '我的任务', group: 'workbench', order: 1 } } },
  { path: '/notifications', component: view('NotificationsView'), meta: { entry: 'bell' } },
  { path: '/subscriptions', component: view('SubscriptionsView'), meta: { entry: 'account' } },

  { path: '/risks', component: view('RisksView'), meta: { nav: { label: '风险事项库', group: 'risks', order: 0 } } },
  // 静态段写在动态段之前。vue-router 自己也会把静态段排在前面，但顺序写对了，
  // 读这段代码的人就不必先知道这件事。
  { path: '/risks/anomalies', component: view('RisksView'), meta: { preset: { view: 'anomaly' }, entry: 'tab' } },
  { path: '/risks/archived', component: view('RisksView'), meta: { preset: { view: 'archived' }, entry: 'tab' } },
  { path: '/risks/:id', component: view('RiskDossierView') },
  // 事件库和风险事项库刻意是两个名字：1,907 条事件，277 条事项。两个页面共用一个
  // 名字，读者第一次对不上数就不会再相信任何一个数。
  { path: '/events', component: view('EventsView'), meta: { nav: { label: '事件库', group: 'risks', order: 1 } } },
  { path: '/events/:id', component: view('EventDetailView') },
  // 旧地址仍然有人存着（使用指南、全局检索结果里都链过）。重定向而不是留一个
  // 内容重复的页面：同一批 277 条在两个页面上各有一个名字，是比断链更坏的事。
  { path: '/alerts', redirect: '/risks' },

  // 公司档案只有详情页，不挂侧栏：库里两家公司，两行的列表是空壳。入口是事项库、
  // 事件库、研判档案里那个公司名。
  { path: '/companies/:id', component: view('CompanyView') },
  { path: '/issues', component: view('IssuesView'), meta: { nav: { label: '专题跟踪', group: 'company', order: 0 } } },
  { path: '/issues/:id', component: view('IssueDetailView') },
  { path: '/timeline', component: view('TimelineView'), meta: { nav: { label: '事件脉络', group: 'company', order: 1 } } },

  // 「专项核查」此前指着 /review——那一页是**模型标注**的复核队列，与业务事项的核查
  // 不是一回事。名字还给真正的专项核查，/review 改叫「标注复核」、挪进系统管理：
  // 它问的是「模型判得对不对」，属于规则质量那一侧。
  { path: '/investigations', component: view('InvestigationsView'), meta: { nav: { label: '专项核查', group: 'handling', order: 0 } } },
  { path: '/outbox', component: view('OutboxView'), meta: { nav: { label: '上报与推送记录', group: 'handling', order: 1 } } },

  { path: '/ops/quality', component: view('OverviewView'), meta: { nav: { label: '数据质量总览', group: 'data', order: 0 } } },
  { path: '/ingestion', component: view('IngestionView'), meta: { nav: { label: '多源信息采集与事件转化', group: 'data', order: 1 } } },
  { path: '/elements', component: view('ElementsView'), meta: { nav: { label: '事件要素抽取与标准化', group: 'data', order: 2 } } },
  { path: '/claims', component: view('ClaimsView'), meta: { nav: { label: '证据核验明细', group: 'data', order: 3 } } },
  { path: '/diffs', component: view('DiffsView'), meta: { nav: { label: '关键信息变化', group: 'data', order: 5 } } },
  { path: '/duplicates', component: view('DuplicatesView'), meta: { nav: { label: '跨源传播聚合', group: 'data', order: 6 } } },
  { path: '/propagation', component: view('PropagationView'), meta: { nav: { label: '信息传播脉络', group: 'data', order: 7 } } },
  { path: '/profiling', component: view('ProfilingView'), meta: { nav: { label: '报道属性与可信分析', group: 'data', order: 4 } } },
  { path: '/master-data', component: view('MasterDataView'), meta: { nav: { label: '来源与词库', group: 'system', order: 1 } } },

  { path: '/benchmark', component: view('BenchmarkView'), meta: { nav: { label: '研判质量评测', group: 'data', order: 8 } } },
  { path: '/review', component: view('ReviewView'), meta: { nav: { label: '标注复核', group: 'system', order: 0 } } },
  { path: '/audit', component: view('AuditView'), meta: { nav: { label: '审计日志', group: 'system', order: 2 } } },
  { path: '/users', component: view('UsersView'), meta: { nav: { label: '账号权限', group: 'system', order: 3 } } },

  // 认不出的地址一律回首页，必须是最后一条。此前没有这一条：新设备从 `/index.html`、
  // 旧书签或转发来的旧链接进来，守卫把它记成 `?redirect=`，登录后原样带回去——外壳
  // 画出来了、正文一片空白，得自己点「工作台 → 风险预警中心」才进得去。
  // 回首页而不是给一张 404：进来的人要的是平台，不是一句「找不到」。
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

/**
 * 把路由表投影成侧栏要渲染的形状。
 *
 * @param all 路由记录数组（`routes`，或测试里 `router.getRoutes()` 的返回）。
 * @returns NAV_GROUPS 的顺序里每组带上自己的链接。空组不会出现：一个没有任何页面
 *   的标题只是噪声。
 */
export function navigation(all) {
  const entries = all
    .filter((route) => route.meta?.nav)
    .map((route) => ({ to: route.path, ...route.meta.nav }))
  return NAV_GROUPS
    .map((group) => ({
      ...group,
      links: entries
        .filter((entry) => entry.group === group.key)
        .sort((a, b) => a.order - b.order),
    }))
    .filter((group) => group.links.length)
}

/**
 * 当前地址属于哪一组——手风琴据此展开。
 *
 * 取「是当前地址前缀的最长那条侧栏链接」：`/risks/abc` 落在「风险事项库」所在的组，
 * `/risks/archived` 同理。`/` 只精确匹配，否则它是所有地址的前缀。找不到（公司档案、
 * 通知箱这类不在侧栏的页）返回 null，由调用方决定保留哪一组。
 */
export function groupOf(path, groups) {
  let best = null
  for (const group of groups) {
    for (const link of group.links) {
      const hit = link.to === '/' ? path === '/' : (path === link.to || path.startsWith(`${link.to}/`))
      if (hit && (!best || link.to.length > best.length)) best = { key: group.key, length: link.to.length }
    }
  }
  return best?.key ?? null
}

const router = createRouter({ history: createWebHistory(), routes })

// Guarding here rather than inside each view keeps an unauthenticated reload on
// a deep link from firing its data requests at all.
router.beforeEach(async (to) => {
  if (!session.ready) await refreshSession()
  if (to.meta.public) return session.authenticated && to.path === '/login' ? '/' : true
  if (!session.authenticated) {
    // Remembering where they were going is the difference between a gate and an
    // obstacle: after signing in they land on the page they asked for.
    return { path: '/login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }
  return true
})

export default router
