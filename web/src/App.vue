<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NConfigProvider, zhCN, dateZhCN } from 'naive-ui'
import { api, companyLabel, formatDate, formatDateTime, loadCompanyLabels } from './api.js'
import { groupOf, navigation } from './router.js'
import { ROLE_LABELS, session, signOut } from './session.js'
import { useNaiveTheme } from './theme/naive.js'
import SearchBox from './components/SearchBox.vue'
import Select from './components/ui/Select.vue'

/** 侧栏是路由表的投影，不是它的第二份副本。
 *
 *  分组与标签写在 `router.js` 的 `meta.nav` 上，这里只负责画。此前这份清单在
 *  `router.js`、本文件和 `app-shell.test.js` 里各存了一份，三份要同时改——而
 *  「新页面没有入口」这件事偏偏只能靠读其中一份副本的断言来拦。
 *
 *  Grouping is presentational only: every link stays visible to every role,
 *  because hiding a control somebody cannot use is a courtesy and the server
 *  refuses the request either way. */
const route = useRoute()
const router = useRouter()
const nav = computed(() => navigation(router.getRoutes()))

/* 手风琴：一次只展开一组。此前六组二十七条常驻展开，侧栏要滚两屏，读者找不到
   「我现在在哪一块」。

   展开哪一组由当前地址决定——换页、刷新、从深链进来，展开的都是当前页所在的那组，
   不是上一次手点的状态。点组标题只切换展开，不跳转：标题是目录，不是页面。
   地址不属于任何组时（公司档案、通知箱）保留原来展开的那组，侧栏不必跟着乱跳。 */
const open = ref(groupOf(route.path, nav.value) || 'workbench')
watch(() => route.path, (path) => {
  const key = groupOf(path, nav.value)
  if (key) open.value = key
})
function toggle(key) {
  open.value = open.value === key ? null : key
}
const THEMES = { light: '浅色', dark: '深色', sepia: '护眼' }

/* 默认深色（2026-10-04 起）。只有读者**亲手选过**的主题才记住，存在 THEME_KEY 下。

   换键而不是沿用旧的 'theme'：旧代码在每次打开页面时都把当前主题写回去，于是每个来过的
   浏览器里都存着一个 'light'——分不出那是有人选的，还是当时的默认值。沿用它，默认改成
   深色就只对从没打开过平台的浏览器生效。换键之后每个人先见到深色，选过一次就一直按他选的。
   index.html 里有一段同样逻辑的内联脚本，在样式生效前就设好 data-theme，免得先闪一下白底；
   两处的键名与默认值由 app-shell.test.js 钉成一致。

   认不出的值（例如早已去掉的「跟随系统」留下的 'auto'）一律落到默认值，而不是写出一个
   三套令牌都不匹配的 data-theme。 */
const THEME_KEY = 'theme-choice'
const DEFAULT_THEME = 'dark'
const stored = localStorage.getItem(THEME_KEY)
const theme = ref(stored in THEMES ? stored : DEFAULT_THEME)

function applyTheme(value) {
  document.documentElement.setAttribute('data-theme', value)
}

/* 曾经是一颗轮换按钮：要到第三次点击才知道有几套，也没法直接跳到想要的那套。
   下拉把三个选项一次摊开。

   这里本来手搓了一份按钮 + listbox——理由仍然成立（原生下拉的列表由操作系统
   绘制，CSS 够不着），但那一份没有方向键，也没有首字母跳转。同一个控件在站里
   有两份实现，其中一份的可访问性更差，这正是共享组件要消掉的东西。 */
const themeOptions = Object.entries(THEMES).map(([value, label]) => ({ value, label }))

function pickTheme(value) {
  theme.value = value
  applyTheme(value)
  localStorage.setItem(THEME_KEY, value)
}

// The component library reads its colours from the same ref, through the tokens
// in styles.css.  It observes; the logic above stays the owner of the setting.
const { theme: naiveTheme, themeOverrides } = useNaiveTheme(theme)

async function leave() {
  await signOut()
  router.replace('/login')
}

const chrome = computed(() => !route.meta.bare)

// What is waiting, shown where every page can see it — an inbox reachable only
// from its own page is one nobody opens.  Refreshed on navigation rather than on
// a timer: nothing here is live, and polling a static corpus is noise.
const unread = ref(0)
// Degradation has to be visible on every page: a figure produced while the
// cloud model was unreachable is not wrong, but a reader must not have to guess
// which kind of figure they are looking at.
const gateway = ref(null)
// 库比代码旧：平台改不了它，但页面必须说出来——否则读者看到的是某一页 500，
// 而横幅上一切正常。
const schema = ref(null)

async function countUnread() {
  if (!session.authenticated) return
  try {
    unread.value = (await api.notifications({ limit: 1 })).unread
    const status = await api.systemStatus()
    gateway.value = status.analysis_service
    schema.value = status.needs_upgrade || null
  } catch {
    unread.value = 0
  }
}

watch(() => [route.path, session.authenticated], countUnread, { immediate: true })

/* 铃铛展开的是一个抽屉，不是一页：点一条就直达那条事项，看完不必再从通知页退回来
   （修改思路 v2 §八）。完整的通知箱仍在，抽屉底部一条链接进去——翻旧通知、按类型看
   构成，那一页做得比抽屉好。 */
const drawer = ref(false)
const latest = ref([])
const DRAWER_KINDS = { alert: '风险预警', review_task: '复核任务', handoff: '交接给我' }

async function toggleDrawer() {
  drawer.value = !drawer.value
  if (!drawer.value) return
  try {
    latest.value = (await api.notifications({ limit: 8 })).items
  } catch {
    latest.value = []
  }
}

async function openNotification(item) {
  drawer.value = false
  if (!item.read_at) {
    try {
      await api.markNotificationsRead([item.notification_id])
    } catch { /* 读回执失败不挡住跳转 */ }
  }
  countUnread()
}

/* 窄屏（≤760px）时侧栏收成抽屉，由工具条左端的「菜单」打开。此前 400px 宽时侧栏
   照旧占 208px，正文只剩一半。宽屏下这个状态不起作用：按钮与遮罩都由样式藏起，
   侧栏的样子与此前逐像素相同。 */
const navOpen = ref(false)
const menuButton = ref(null)

function closeOnEscape(event) {
  if (event.key !== 'Escape') return
  drawer.value = false
  if (navOpen.value) {
    navOpen.value = false
    // 焦点还给打开它的按钮，否则它停在一个已经藏起来的链接上。
    menuButton.value?.focus()
  }
}
watch(() => route.path, () => { drawer.value = false; navOpen.value = false })

// A session can also end mid-visit, when a request comes back 401; the guard
// only runs on navigation, so the sign-out has to be followed here too.
watch(() => session.authenticated, (authenticated) => {
  if (!authenticated && route.path !== '/login') {
    router.replace({ path: '/login', query: route.fullPath === '/' ? {} : { redirect: route.fullPath } })
  }
})

onBeforeUnmount(() => document.removeEventListener('keydown', closeOnEscape))

onMounted(() => {
  document.addEventListener('keydown', closeOnEscape)
  applyTheme(theme.value)
  // 公司名从配置来，一次取回；取回之前各页显示的是公司标识，取回后自动换成名字。
  loadCompanyLabels()
})
</script>

<template>
  <!-- `abstract` renders no element of its own, so the flex layout below and the
       chrome-less routes are laid out exactly as they were before. -->
  <NConfigProvider :theme="naiveTheme" :theme-overrides="themeOverrides"
                   :locale="zhCN" :date-locale="dateZhCN" abstract>
  <div :class="chrome ? 'layout' : ''">
    <!-- 有侧栏时主题切换在正文右上角的工具条里（见下方 .topbar），与通知铃铛并排。
         它走过两个位置：先是一颗浮在右上角的按钮，压住页面自己的操作按钮；再是侧栏底部
         的账号块，不压任何东西，但没人想到去那里找。工具条占一行而不浮动，两头都避开了。

         没有侧栏时它仍然浮在右上角，且这一支**不挂在 `chrome` 下面**——登录页与
         报告页没有侧栏，但它们同样有浅深两个样子，切换的入口不该只在门里面。
         这条约束由 app-shell.test.js 钉着。 -->
    <div v-if="!chrome" class="theme floating">
      <Select
        :model-value="theme" :options="themeOptions" label="配色主题"
        variant="pill" align="right" @update:model-value="pickTheme" />
    </div>

    <nav v-if="chrome" id="site-nav" class="sidebar" :data-open="navOpen || null">
      <div class="brand">上市公司事件智能平台</div>
      <!-- 这里曾经有过一行副标题，写着已被撤销的说法，于是连同形式一起删掉了。
           形式没错，错的是内容——测试至今钉着全站不得出现那两个词。 -->
      <div class="brand-strap">媒体报道与公司披露逐条核验</div>
      <!-- 检索框在分组之上：它不属于命题的六大任务里的任何一项，是进入这些任务的
           另一条路——目的性强的时候不必先猜要去哪一页。 -->
      <SearchBox />

      <div class="group" v-for="group in nav" :key="group.key"
           :class="{ open: open === group.key }">
        <button type="button" class="group-title" :aria-expanded="open === group.key"
                :aria-controls="`nav-${group.key}`" :title="group.hint"
                @click="toggle(group.key)">
          <span>{{ group.title }}</span>
          <span class="chevron" aria-hidden="true">{{ open === group.key ? '▾' : '▸' }}</span>
        </button>
        <!-- v-show 而不是 v-if：收起的链接仍在 DOM 里，「每个页面都有入口」由测试逐条
             核对的是真实渲染出的 href，不是一份副本。 -->
        <div v-show="open === group.key" :id="`nav-${group.key}`" class="group-links">
          <div v-if="group.hint" class="group-hint">{{ group.hint }}</div>
          <RouterLink v-for="link in group.links" :key="link.to" class="nav-link" :to="link.to">
            {{ link.label }}
          </RouterLink>
        </div>
      </div>

      <div class="foot">
        <!-- 使用帮助与订阅设置是「关于我这次使用」的东西，和账号待在一起。 -->
        <div class="foot-links">
          <RouterLink to="/guide" class="foot-link">？使用帮助</RouterLink>
          <RouterLink to="/subscriptions" class="foot-link">订阅设置</RouterLink>
        </div>
        <div class="account">
          <span class="who">
            {{ session.username || '—' }}
            <span v-if="session.role" class="role">{{ ROLE_LABELS[session.role] || session.role }}</span>
          </span>
          <button class="link" @click="leave">退出</button>
        </div>
      </div>
    </nav>
    <!-- 只在窄屏抽屉打开时出现；点它就收起。宽屏下样式把它藏起。 -->
    <div v-if="chrome && navOpen" class="nav-scrim" aria-hidden="true" @click="navOpen = false" />
    <main v-if="chrome" class="main">
      <!-- 右上角工具条：通知与主题。占一行、不浮动——此前的浮动主题按钮压在各页右上角的
           操作按钮（「导出风险简报」）上，正文只好常年多留 52px 顶部空白来躲它；后来收进了
           侧栏底部，又太难找。一行实实在在的工具条两头都不占：它在大家习惯找的位置，
           又把页标题整体往下推，而不是盖住它。 -->
      <div class="topbar">
        <button ref="menuButton" type="button" class="menu" :aria-expanded="navOpen"
                aria-controls="site-nav" aria-label="导航菜单" @click="navOpen = !navOpen">
          <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" fill="none"
               stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
            <path d="M4 6h16M4 12h16M4 18h16" />
          </svg>
          <span>菜单</span>
        </button>
        <div class="bell-wrap">
          <button type="button" class="bell" :aria-expanded="drawer" aria-controls="notice-drawer"
                  :aria-label="unread ? `通知，${unread} 条未读` : '通知'" title="通知"
                  @click="toggleDrawer">
            <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" fill="none"
                 stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
              <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
            </svg>
            <span v-if="unread" class="badge">{{ unread > 99 ? '99+' : unread }}</span>
          </button>
          <div v-if="drawer" id="notice-drawer" class="drawer" role="dialog" aria-label="最新通知">
            <div class="drawer-head">
              <b>最新通知</b>
              <button type="button" class="link" @click="drawer = false">关闭</button>
            </div>
            <RouterLink v-for="item in latest" :key="item.notification_id" :to="item.link"
                        class="notice" :class="{ unread: !item.read_at }" @click="openNotification(item)">
              <span class="notice-meta">
                {{ DRAWER_KINDS[item.kind] || item.kind }} · {{ companyLabel(item.company_id) }} ·
                {{ formatDate(item.created_at) }}
              </span>
              <span class="notice-title">{{ item.title }}</span>
              <span v-if="item.kind === 'handoff'" class="notice-body">{{ item.body }}</span>
            </RouterLink>
            <p v-if="!latest.length" class="muted small">没有通知。</p>
            <div class="drawer-foot">
              <RouterLink to="/notifications" @click="drawer = false">查看全部通知 →</RouterLink>
              <RouterLink to="/subscriptions" @click="drawer = false">订阅设置</RouterLink>
            </div>
          </div>
        </div>
        <div class="theme">
          <Select
            :model-value="theme" :options="themeOptions" label="配色主题"
            variant="pill" align="right" @update:model-value="pickTheme" />
        </div>
      </div>
      <div v-if="schema" class="degraded">
        平台数据需要升级，部分页面可能缺少内容。请联系管理员处理。
      </div>
      <div v-if="gateway && !gateway.ok" class="degraded">
        智能研判服务当前不可用（检测于 {{ formatDateTime(gateway.checked_at) }}）。
        页面展示的是已存储的结论，<b>不受影响</b>；新的批次只做检索与规则可以判定的部分，
        其余留待服务恢复后处理，不会写入任何未经研判的结论。
      </div>
      <RouterView />
    </main>
    <RouterView v-else />
  </div>
  </NConfigProvider>
</template>

<style scoped>
.degraded { border: 1px solid var(--status-warning); border-left-width: 2px; border-radius: 8px;
  padding: 9px 12px; margin-bottom: 12px; font-size: var(--fs-body); background: var(--surface-1); }
.badge { padding: 0 5px; border-radius: 8px; font-size: var(--fs-note); line-height: 1.5;
  background: var(--status-serious); color: var(--surface-0, #fff); }
.brand { white-space: nowrap; }
/* 工具条占一行，把页标题整体往下推；正文顶部留白相应从 28px 收到 14px，页标题的位置几乎不动。 */
.main { padding-top: 14px; }
.topbar { display: flex; justify-content: flex-end; align-items: center; gap: 10px;
  min-height: 34px; margin-bottom: 6px; }
.bell-wrap { position: relative; }
/* 菜单按钮与遮罩只属于窄屏，断点与 styles.css 里侧栏变抽屉的那一条相同。 */
.menu { display: none; margin-right: auto; align-items: center; gap: 6px; padding: 5px 12px 5px 9px;
  min-height: 0; border: 1px solid var(--border); border-radius: 999px; background: var(--surface-1);
  color: var(--text-secondary); font-size: var(--fs-aux); cursor: pointer; }
.menu:hover, .menu[aria-expanded="true"] { background: var(--hover-wash); color: var(--text-primary); }
.nav-scrim { display: none; }
@media (max-width: 760px) {
  .menu { display: inline-flex; }
  .nav-scrim { display: block; position: fixed; inset: 0; z-index: 45; background: rgb(0 0 0 / 0.32); }
}
.bell { position: relative; display: inline-flex; align-items: center; padding: 6px;
  min-height: 0; border: 1px solid var(--border); border-radius: 999px; background: var(--surface-1);
  color: var(--text-secondary); cursor: pointer; }
.bell:hover, .bell[aria-expanded="true"] { background: var(--hover-wash); color: var(--text-primary); }
/* 抽屉从铃铛下方向左展开，压在正文上方而不推开它：看一眼就关，不该让正文跳一下。 */
.drawer { position: absolute; top: calc(100% + 8px); right: 0; z-index: 40; width: 360px;
  max-width: calc(100vw - 32px); max-height: calc(100vh - 80px); overflow-y: auto;
  padding: var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--surface-1); box-shadow: 0 8px 24px rgb(0 0 0 / 0.16); }
.drawer-head, .drawer-foot { display: flex; justify-content: space-between; align-items: baseline; }
.drawer-head { margin-bottom: var(--sp-2); }
.drawer-foot { margin-top: var(--sp-2); padding-top: var(--sp-2); border-top: 1px solid var(--gridline);
  font-size: var(--fs-aux); }
.notice { display: flex; flex-direction: column; gap: 2px; padding: 8px; border-radius: 6px;
  color: var(--text-primary); }
.notice:hover { background: var(--hover-wash); text-decoration: none; }
.notice.unread { box-shadow: inset 2px 0 0 var(--status-serious); }
.notice-meta { font-size: var(--fs-note); color: var(--text-muted); }
.notice-title { font-size: var(--fs-aux); }
.notice-body { font-size: var(--fs-note); color: var(--text-secondary); }
.bell .badge { position: absolute; top: -6px; left: 18px; }
.foot-links { display: flex; gap: 12px; padding: 0 10px 8px; }
.foot-link { font-size: var(--fs-aux); color: var(--text-secondary); }
.foot-link.router-link-active { color: var(--series-1); font-weight: 600; }
/* `margin-top: auto` pins it to the bottom while the nav fits, and lets it
   follow the last group once the nav overflows — either way it never lands on
   top of a link. */
.foot { margin-top: auto; padding-top: 18px; }
.account {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 0 10px 8px;
  font-size: var(--fs-aux);
  color: var(--text-secondary);
}
.who { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.role { color: var(--text-muted); font-size: var(--fs-note); margin-left: 4px; }
.link { border: none; background: none; padding: 0; min-height: 0; font-size: var(--fs-aux); color: var(--series-1); }
.link:hover { background: none; text-decoration: underline; }
/* 固定在视口右上角。`.main` 因此多留了顶部内边距，否则详情页 PageHeader 的
   「导出报告」正好压在它下面。 */
.theme.floating { position: fixed; top: 12px; right: 18px; z-index: 30; }
/* The select needs a name for assistive tech; sighted readers have three
   labelled options and do not need a fourth word above them. */
.theme .sr {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}




/* 报告页要打印，浮在纸上的按钮不该跟着印出来。 */
@media print { .theme { display: none; } }
.group { margin-bottom: 2px; }
/* 一级标题是按钮：字号比链接高一档、字重更重；展开的那组只加一层淡底，不用重色块——
   最强的强调留给当前页那一条（styles.css 的 .router-link-active）。 */
.group-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 0;
  padding: 8px 10px;
  border: none;
  border-radius: 6px;
  background: none;
  font-size: var(--fs-body);
  font-weight: 600;
  color: var(--text-primary);
  text-align: left;
  cursor: pointer;
}
.group-title:hover { background: var(--hover-wash); }
.group.open > .group-title { background: var(--hover-wash); }
.chevron { color: var(--text-muted); font-size: var(--fs-note); }
/* 二级整体右缩一档，与一级拉开层级。 */
.group-links { padding: 2px 0 6px 10px; }
/* 大白话比任务名再淡一档：它是注解，不该和它注解的东西抢。 */
.group-hint {
  font-size: var(--fs-note);
  color: var(--text-muted);
  opacity: 0.75;
  padding: 2px 10px 4px;
  line-height: 1.35;
}
.brand-strap {
  font-size: var(--fs-note);
  color: var(--text-muted);
  padding: 0 10px 14px;
  margin-top: -14px;
  line-height: 1.3;
}
.sidebar { overflow-y: auto; }
</style>
