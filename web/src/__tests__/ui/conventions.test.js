/** Conventions that are cheaper to assert than to remember.
 *
 *  Each of these encodes a defect this refactor actually found, in a form that
 *  fails if it comes back. They read the view sources as text rather than
 *  mounting anything: what is being checked is a property of the code, not of a
 *  render.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// fileURLToPath, not URL.pathname: under the jsdom environment the latter comes
// back without the leading directories and every read misses.
const VIEWS = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'views')
const source = (name) => readFileSync(join(VIEWS, name), 'utf8')
const views = () => readdirSync(VIEWS).filter((name) => name.endsWith('.vue'))

/** Pages migrated onto the shared layer so far. The list grows with the
 *  migration; a page is added here when it is converted, and these rules then
 *  hold for it forever. */
const MIGRATED = [
  'OverviewView.vue', 'DiffsView.vue', 'IssuesView.vue', 'EventsView.vue', 'ClaimsView.vue',
  'DuplicatesView.vue', 'ElementsView.vue', 'IngestionView.vue', 'ProfilingView.vue',
  'BenchmarkView.vue', 'OutboxView.vue', 'SubscriptionsView.vue',
  'NotificationsView.vue', 'MasterDataView.vue', 'TimelineView.vue', 'PropagationView.vue', 'AuditView.vue',
  'EventDetailView.vue', 'IssueDetailView.vue', 'ReviewView.vue', 'ReportView.vue',
  'GuideView.vue',
  // 业务化重组新建的三页。AlertsView 退役：它的五项能力分别落到了风险事项库
  // 与研判档案里，而两个页面显示同一批 277 条、各有一个名字，比断链更坏。
  'RiskCenterView.vue', 'RisksView.vue', 'RiskDossierView.vue',
  // 专项核查工作视图（清单第 3 项）。
  'InvestigationsView.vue',
  // 公司档案（清单第 4 项）。
  'CompanyView.vue',
]

describe('迁移后的页面', () => {
  it('不再自己拼公司选项——第三家公司不该要求改七个文件', () => {
    for (const name of MIGRATED) {
      expect(source(name), name).not.toMatch(/<option[^>]*value="cxmt"/)
      expect(source(name), name).not.toMatch(/<option[^>]*value="wuxi_apptec"/)
    }
  })

  it('不再各写各的加载态', () => {
    // The words themselves, not just the class name: seven of these were
    // `<div class="muted">加载中…</div>` inside a detail drawer, which the class
    // check let through. They all say the same thing, so one component says it.
    for (const name of MIGRATED) {
      expect(source(name), name).not.toMatch(/class="loading"/)
      expect(source(name), `${name} 自己写了加载文案，应当用 ui/Loading`)
        .not.toMatch(/加载中/)
    }
  })

  it('不把公司筛选默认成某一家——那会让半个语料悄悄消失', () => {
    // DiffsView and ClaimsView both shipped defaulting to cxmt with no way to
    // tell from the page that anything was missing.
    for (const name of MIGRATED) {
      expect(source(name), name).not.toMatch(/company:\s*'(cxmt|wuxi_apptec)'/)
    }
  })
})

describe('全部页面', () => {
  it('表格只从 ui/DataTable 出来——报告页与图表的表格视图除外', () => {
    // The report page's tables are its print layout, and EChart's is the
    // accessible alternative to the chart it sits under. Everything else that
    // renders rows and columns goes through the shared component, or the next
    // person adds a twelfth table with its own idea of density.
    for (const name of views()) {
      if (name === 'ReportView.vue') continue
      expect(source(name), `${name} 自己写了 <table>`).not.toMatch(/<table/)
    }
  })


  it('模板里没有字面 Markdown 强调', () => {
    // `**text**` in a Vue template renders with its asterisks showing. It
    // shipped once in ClaimsView's notice and was nearly reintroduced in the
    // outbox header.
    for (const name of views()) {
      // 到 `</template>` 为止。此前取的是 `<template>` 之后的**全部**内容，
      // 于是 `<style>` 里的一句 CSS 注释也被当成模板判了违规——而一条 CSS
      // 注释永远不会把星号显示给任何人。断言得对准缺陷本身。
      const template = (source(name).split('<template>')[1] || '').split('</template>')[0]
      const withoutComments = template.replace(/<!--[\s\S]*?-->/g, '')
      expect(withoutComments, `${name} 模板中出现字面 **强调**`)
        .not.toMatch(/\*\*[^*\n]+\*\*/)
    }
  })

  it('模板里不直接渲染受控枚举字段', () => {
    // Three separate rounds of this: event_types / affected_objects /
    // impact_direction on the demo page, then article_sentiment beside them,
    // then risk_level printed as 「85high」 in two list views and three places
    // in the report. Every one of these fields has a Chinese label map; the
    // template just has to call it.
    //
    // The check is textual and deliberately narrow — it looks for the field
    // interpolated on its own, which is exactly the shape the mistake takes.
    const ENUMS = ['risk_level', 'article_sentiment', 'impact_direction', 'impact_magnitude',
                   'event_types', 'affected_objects', 'issue_code', 'production_mode']
    for (const name of views()) {
      const template = source(name).split('<template>')[1] || ''
      const withoutComments = template.replace(/<!--[\s\S]*?-->/g, '')
      for (const field of ENUMS) {
        // The exact shape of the mistake: the whole interpolation is the field
        // itself, optionally with a dash fallback. Anything wrapping it — a map
        // lookup, a helper call, a join — is a labelled use and passes.
        //
        // Two earlier drafts of this check were wrong in opposite directions.
        // Matching any mention flagged `LABELS[x.production_mode] || x.…`; then
        // excluding text containing `Label(` still flagged `impactText(…)`.
        const bare = new RegExp(
          `\\{\\{\\s*[\\w.?[\\]']*\\.${field}\\s*(\\|\\|\\s*'[^']*'\\s*)?\\}\\}`, 'g')
        const unlabelled = [...withoutComments.matchAll(bare)].map((m) => m[0])
        expect(unlabelled, `${name} 直接渲染了 ${field}：${unlabelled[0]}`).toEqual([])
      }
    }
  })

  it('每个图表都带一句话说明自己在显示什么', () => {
    // EChart requires `label`, so a missing one already fails at runtime under
    // the warning handler — this catches an empty string, which would not.
    for (const name of views()) {
      const text = source(name)
      const labels = text.match(/label="[^"]*"/g) || []
      for (const label of labels) {
        expect(label.length, `${name} 有空的图表说明`).toBeGreaterThan('label=""'.length)
      }
    }
  })
})

describe('标记结构', () => {
  it('定义列表都有 <dd>，不存在只写了一半的 <dl>', () => {
    // ElementsView carried a `<dl class="detail-grid">` containing nothing but
    // `<dt v-for>` printing a raw enum — a draft that was superseded by the list
    // below it and never deleted. The page showed 「未记录 explicit
    // relative_to_publication」 as a stray row above the real Chinese one.
    //
    // A `<dl>` with no `<dd>` is invalid HTML as well as visibly wrong, which
    // makes it checkable without knowing what the page meant to say.
    for (const name of views()) {
      const template = source(name).split('<template>')[1] || ''
      for (const list of template.match(/<dl\b[\s\S]*?<\/dl>/g) || []) {
        expect(list.includes('<dd'), `${name} 有一个只含 <dt> 的 <dl>`).toBe(true)
      }
    }
  })
})

describe('网格里的卡片不自带外边距', () => {
  it('没有落在单个网格子元素上的 margin-top', () => {
    // `.card + .card { margin-top: 0 }` is an adjacent-sibling rule and outranks
    // `section.card { margin-top: 12px }` on specificity — two classes against
    // one class and one element. So the 12px survived on exactly the cards with
    // no preceding sibling: the *first* child of every grid.
    //
    // 命题六项任务's card ① was therefore 12px shorter than ② and ③ with its
    // bottom aligned and its top pushed down, while row two looked even because
    // every card there has a sibling before it. A grid spaces its children with
    // `gap`; a margin on top of that is always wrong and only ever visible on
    // one of them.
    for (const name of views()) {
      // Comments first: this file's own explanation of the bug quotes the
      // selector, and the first draft of this check flagged the comment.
      const styles = (source(name).split('<style')[1] || '').replace(/\/\*[\s\S]*?\*\//g, '')
      const offenders = [...styles.matchAll(/\.card\s*\+\s*\.card[^{]*\{[^}]*margin-top/g)]
      expect(offenders.map((m) => m[0]), `${name} 用相邻兄弟选择器管卡片外边距`)
        .toEqual([])
    }
  })
})

describe('时间口径', () => {
  it('三种口径各有各的说法，抓取时间不冒充发布时间', async () => {
    // 原来是个三元表达式：event_time 之外一律叫「发布时间」，于是 6 条按抓取
    // 时间定位的事件被印成有发布时间。不是漏标签，是标错——一个做脉络追踪的
    // 平台把爬虫跑过的时刻说成稿件发布的时刻，比留空更糟。
    const { TIME_BASIS_LABELS, basisLabel } = await import('../../api.js')
    expect(new Set(Object.values(TIME_BASIS_LABELS)).size).toBe(3)
    expect(basisLabel('fetched_at')).not.toBe(TIME_BASIS_LABELS.published_at)
    expect(basisLabel('fetched_at')).toContain('抓取')
  })

  it('没见过的口径原样透出，而不是归到某一类里', async () => {
    const { basisLabel } = await import('../../api.js')
    expect(basisLabel('something_new')).toBe('something_new')
  })
})

/** 排版令牌。
 *
 *  改之前全站 font-size 有 18 种取值、约 181 处，其中 11 / 11.5 / 12 / 12.5 /
 *  13px 五种挤在 2px 带宽里、占 78% 的用量——五种肉眼分不出来的字号不构成层级，
 *  只是让四十多个文件各写各的。这两条守的是「下一个页面不会又写回 12.5px」。
 *
 *  局限要说在前面：它只查「有没有用令牌」，查不出「用得对不对」——把页标题写成
 *  var(--fs-note) 一样能过。真正的验收是三套主题下的截图与页高/行宽实测。
 */
describe('排版令牌', () => {
  // 只剩两页：登录页封面（一整套自成体系的排版）与打印报告的标题。
  //
  // 2026-09-07 减掉两项。BenchmarkView 与 OverviewView 当初进名单，各自只为一条
  // 字面字号——评测 hero 的 34px 与驾驶舱任务卡的 22px。它们落在 20 到 26 之间的
  // 空档里，而那个空档存在的原因是令牌表里没有「关键结果」这一级。补上
  // --fs-result 与 --fs-hero 之后，两处都接回了令牌，例外随之消失。
  const EXEMPT = ['LoginView.vue', 'ReportView.vue']

  it('样式里不再出现字面字号，只能用 --fs-* 令牌', () => {
    const offenders = []
    for (const name of views()) {
      if (EXEMPT.includes(name)) continue
      for (const block of source(name).match(/<style[^>]*>[\s\S]*?<\/style>/g) || []) {
        for (const hit of block.match(/font-size:\s*[0-9.]+px/g) || []) {
          offenders.push(`${name}: ${hit}`)
        }
      }
    }
    expect(offenders, '字号必须走 var(--fs-*)').toEqual([])
  })

  it('白名单只剩两页，且都不是数据页', () => {
    // 把名单写进断言而不只是写进注释：豁免是会长的，而每多一页，「字号走令牌」
    // 这条约定就少管一页。两项都是排版自成体系的页面（封面、打印稿），任何数据
    // 页要再进来，得先在这里改一行并说明理由。
    expect(EXEMPT).toEqual(['LoginView.vue', 'ReportView.vue'])
  })

  it('关键结果不小于正文', () => {
    // 防的是这次修掉的那个具体缺陷原样回来：风险分渲染成 12px，比解释它的那句
    // 摘要（13px）还小，而这张表自己写着「比它所解释的内容小一号」。
    const sheet = readFileSync(join(VIEWS, '..', 'styles.css'), 'utf8')
    const px = (name) => {
      const hit = sheet.match(new RegExp(`--${name}:\\s*([0-9.]+)px`))
      expect(hit, `没有定义 --${name}`).not.toBeNull()
      return Number(hit[1])
    }
    expect(px('fs-result'), '关键结果比正文还小').toBeGreaterThan(px('fs-body'))
    expect(px('fs-hero')).toBeGreaterThan(px('fs-display'))
    expect(px('fs-display')).toBeGreaterThan(px('fs-result'))
  })

  it('styles.css 自己也算数', () => {
    // 上面那条只扫 views/*.vue 的 <style>，所以令牌清扫那一轮漏掉了这张表本身：
    // .evidence-head 12px、.evidence-text 13px、.detail-grid 13px、.brand 15px
    // 四处字面字号一直活着，而它们是全站共用的类。
    const sheet = readFileSync(join(VIEWS, '..', 'styles.css'), 'utf8')
    expect(sheet.match(/font-size:\s*[0-9.]+px/g) || [],
           'styles.css 里的字号也必须走 var(--fs-*)').toEqual([])
  })

  it('.small 与 .footnote 不再在组件里各写一份', () => {
    // 改之前 .small 在 27 个文件里逐字重复，.footnote 在 3 个。它们现在是
    // styles.css 里的全局类；再在组件里声明一遍就是新的分叉。
    const offenders = []
    for (const name of views()) {
      for (const block of source(name).match(/<style[^>]*>[\s\S]*?<\/style>/g) || []) {
        if (/^\.small\s*\{/m.test(block) || /^\.footnote\s*\{/m.test(block)) offenders.push(name)
      }
    }
    expect(offenders, '.small / .footnote 已是全局类').toEqual([])
  })
})

/** 每一页都说得出自己在回答什么问题——而且只说一句。
 *
 *  这条约定有前情：2026-09-06 各页的 PageHeader 导语与折叠口径被整体撤下（模板净减
 *  2,592 字），理由是它横在读者和数据中间。那个判断是对的，代价写在
 *  docs/界面体检记录.md §4.3。2026-09-07 按产品决定把导语加回来，形态换了：一句、
 *  不超过 40 字、只回答「这一页在回答什么问题」，方法学与「这不是什么」归使用指南页，
 *  不再摊回每一页。
 *
 *  所以这里钉的是两件事，缺一件这条约定就会退回原样：有没有那一句，和它有没有长回
 *  一段。守不住的是「写得对不对」——那需要一个读得懂中文的读者。 */
describe('每页一句导语', () => {
  const BARE = ['LoginView.vue', 'ReportView.vue']   // 没有 PageHeader 的两页
  const header = (text) => text.match(/<PageHeader[\s\S]{0,400}?(?:\/>|>)/)

  it('有 PageHeader 的页面都传了 note', () => {
    for (const name of views().filter((view) => !BARE.includes(view))) {
      const block = header(source(name))
      expect(block, `${name} 没有 PageHeader`).not.toBeNull()
      expect(block[0], `${name} 的 PageHeader 没有 note`).toMatch(/\snote="/)
    }
  })

  it('导语是一句，不是一段', () => {
    for (const name of views().filter((view) => !BARE.includes(view))) {
      const note = header(source(name))[0].match(/\snote="([^"]*)"/)
      expect(note[1].length, `${name} 的导语 ${note[1].length} 字，超过 40`)
        .toBeLessThanOrEqual(40)
    }
  })
})

/** 一个量一个名字，一句解释一份文本。
 *
 *  加通俗解释之前先撞上的问题：`independent_source_count` 这一个字段，在议题台账
 *  上列头叫「独立来源」、旁边的说明和图注叫「独立信源」；另有 `independent_components`
 *  叫「独立报道链」、由它算出的可信度权重项叫「独立佐证」。两个量、四个名字，其中
 *  一页自己用了两个。不先统一，解释就会和它旁边的标签打架。
 *
 *  统一后：**独立信源** = 剔除转载与改写后剩下的报道数（可信度的分母）；
 *  **发布方** = 有几家发出（COUNT DISTINCT source_id）。两个量，两个名字。
 */
describe('术语只有一个名字', () => {
  const sources = () => views().map((name) => [name, source(name)])
    .concat([['glossary.js', readFileSync(join(VIEWS, '..', 'glossary.js'), 'utf8')]])

  it('「独立来源」「独立佐证」「独立报道链」三个旧说法不再出现', () => {
    const offenders = []
    for (const [name, text] of sources()) {
      for (const word of ['独立来源', '独立佐证', '独立报道链']) {
        if (text.includes(word)) offenders.push(`${name}: ${word}`)
      }
    }
    expect(offenders, '同一个量又长出了第二个名字').toEqual([])
  })

  it('悬停与名词表取自同一份文本', () => {
    // 界面体检记录 §五 批评过「同一句话在前后端各存一份」。这里守的是同一件事：
    // GuideView 不许自己再抄一份码的解释，必须 import glossary.js。
    const guide = source('GuideView.vue')
    expect(guide, 'GuideView 没有从 glossary.js 取解释').toMatch(/from '\.\.\/glossary\.js'/)
  })
})

/** 「专题」与「议题」是同一个东西。侧栏、检索、预警标题早就叫专题，专题页自己却叫
 *  「议题台账」「议题概况」——2026-09-23 统一成专题。注释与内部字段名（issue_code）不算。 */
describe('一个量只有一个名字：专题', () => {
  it('页面文字里不再出现「议题」', () => {
    const comment = /^\s*(\/\/|\*|\/\*|<!--)/
    const offenders = []
    for (const name of views()) {
      source(name).split('\n').forEach((line, index) => {
        if (line.includes('议题') && !comment.test(line)) offenders.push(`${name}:${index + 1}`)
      })
    }
    expect(offenders, '界面上统一叫「专题」').toEqual([])
  })
})

/** 修改思路 v2 这一轮又找到三处「一页两个名字」：侧栏叫事件库、页标题叫事件台账；处置状态
 *  「待上报」改成了「待上级复核」；研判档案改名风险研判工作台。注释不算——注释里记的正是
 *  「此前叫什么」。 */
describe('一个量只有一个名字：这一轮改过名的三处', () => {
  it.each([
    ['事件台账', '事件库'],
    ['待上报', '待上级复核'],
    ['研判档案', '风险研判工作台'],
  ])('页面文字里不再出现「%s」（现在叫「%s」）', (old) => {
    const comment = /^\s*(\/\/|\*|\/\*|<!--)/
    const offenders = []
    for (const name of views()) {
      source(name).split('\n').forEach((line, index) => {
        if (line.includes(old) && !comment.test(line)) offenders.push(`${name}:${index + 1}`)
      })
    }
    expect(offenders).toEqual([])
  })
})

/** 点侧栏的「数据质量总览」，落到的页面必须叫数据质量总览。
 *
 *  2026-09-23 核对时，21 个侧栏入口里有 13 个的页标题与入口名不同——「数据质量总览」进去
 *  叫「驾驶舱」，「模型与规则质量」进去叫「评测中心」。每一处都是同一个页面两个名字，读者
 *  点完要先确认「我是不是点错了」。侧栏的名字是业务化改名时定下的，以它为准。 */
describe('一个页面只有一个名字：侧栏入口 = 页标题', () => {
  const router = readFileSync(join(VIEWS, '..', 'router.js'), 'utf8')
  const entries = [...router.matchAll(/component: view\('(\w+)'\), meta: \{[^}]*nav: \{ label: '([^']+)'/g)]
    .map(([, view, label]) => [view, label])

  it('核对到的入口不是空的', () => {
    expect(entries.length).toBeGreaterThan(15)
  })

  it.each(entries)('%s 的页标题是「%s」', (view, label) => {
    const title = source(`${view}.vue`).match(/<PageHeader\s+title="([^"]+)"/)
    expect(title, `${view} 没有字面量的 PageHeader title`).not.toBeNull()
    expect(title[1]).toBe(label)
  })
})

/** 每个输入框都要有可访问名（外部测试 I-07）。
 *
 *  placeholder 不算名字：读屏不把它当字段名，一输入它也就消失了。账号权限页的
 *  两个密码框就是这么被报出来的；同一轮排查又在各列表页找到十一个只有
 *  placeholder 的框。可以包在 <label> 里，或带 aria-label / aria-labelledby，
 *  或者用 id 加对应的 <label for>。 */
describe('输入框都有可访问名', () => {
  const ROOT = join(VIEWS, '..')
  const vueFiles = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return entry.name === '__tests__' ? [] : vueFiles(path)
    return entry.name.endsWith('.vue') ? [path] : []
  })

  function unnamed(text) {
    const found = []
    for (const match of text.matchAll(/<(input|textarea)\b[^>]*>/g)) {
      const tag = match[0]
      if (/type="(checkbox|radio|hidden)"/.test(tag)) continue
      if (/aria-label(ledby)?=|:aria-label(ledby)?=/.test(tag)) continue
      const id = tag.match(/\sid="([^"]+)"/)
      if (id && text.includes(`for="${id[1]}"`)) continue
      const before = text.slice(0, match.index)
      if (before.lastIndexOf('<label') > before.lastIndexOf('</label>')) continue
      found.push(tag.replace(/\s+/g, ' ').slice(0, 90))
    }
    return found
  }

  it('扫描到了足够多的页面', () => {
    expect(vueFiles(ROOT).length).toBeGreaterThan(40)
  })

  it('没有只靠 placeholder 的输入框', () => {
    const offenders = vueFiles(ROOT).flatMap((path) =>
      unnamed(readFileSync(path, 'utf8')).map((tag) => `${path.slice(ROOT.length + 1)}: ${tag}`))
    expect(offenders).toEqual([])
  })

  it('规则本身能识别出未命名的框', () => {
    expect(unnamed('<input v-model="q" placeholder="搜索" />')).toHaveLength(1)
    expect(unnamed('<label>名称<input v-model="q" /></label>')).toHaveLength(0)
    expect(unnamed('<input v-model="q" aria-label="搜索" />')).toHaveLength(0)
  })
})
