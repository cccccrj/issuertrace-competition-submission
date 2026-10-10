/**
 * 逐页截图与版式体检。
 *
 * 写这个脚本的直接原因：图例被画在图里、热力图整块颜色通道失效、十二处裸枚举
 * ——这些缺陷全都通不过任何单元测试，因为 option 对象是对的、组件也渲染成功
 * 了。只有把页面画出来看才发现得了。
 *
 * 它顺带做两项机器能判的检查：控制台报错，和横向溢出（这套界面里任何横向滚动
 * 条都是版式事故）。两项都干净时退出码为 0，可以直接挂进 CI。
 *
 *   npm run shots                      # 全部页面，浅色
 *   npm run shots -- --theme dark      # 深色下再看一遍
 *   npm run shots -- --full /alerts    # 指定页面，整页而非首屏
 *   npm run shots -- --width 2560 --height 1300   # 2K 屏
 *
 * 需要一个 Chromium。优先用 PLAYWRIGHT_CHROMIUM / CHROME 指定，其次找
 * playwright 的缓存目录，最后试系统里的 chromium。
 */
import { existsSync, mkdirSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:8000'
const USER = process.env.SHOT_USER || 'reviewer'
const PASSWORD = process.env.SHOT_PASSWORD

/** 默认覆盖每一条带外壳的路由。两个详情页的 id 从接口现取——写死的 id 会随
 *  下一次重跑处理层失效，那时脚本会静默截出一张 404。 */
const ROUTES = [
  '/guide', '/search',
  '/', '/todo', '/notifications', '/subscriptions',
  '/risks', '/risks/anomalies', '/risks/archived', '/events',
  '/issues', '/timeline',
  '/investigations', '/review', '/outbox',
  '/ops/quality', '/ingestion', '/elements', '/claims', '/diffs',
  '/duplicates', '/propagation', '/profiling', '/master-data',
  '/benchmark', '/audit', '/users',
]

function findChromium() {
  for (const candidate of [process.env.PLAYWRIGHT_CHROMIUM, process.env.CHROME]) {
    if (candidate && existsSync(candidate)) return candidate
  }
  const cache = join(homedir(), '.cache', 'ms-playwright')
  if (existsSync(cache)) {
    for (const dir of readdirSync(cache).filter((name) => name.startsWith('chromium-'))) {
      for (const rel of ['chrome-linux64/chrome', 'chrome-linux/chrome',
                         'chrome-mac/Chromium.app/Contents/MacOS/Chromium']) {
        const path = join(cache, dir, rel)
        if (existsSync(path)) return path
      }
    }
  }
  for (const path of ['/usr/bin/chromium', '/usr/bin/chromium-browser',
                      '/usr/bin/google-chrome']) {
    if (existsSync(path)) return path
  }
  return null
}

const args = process.argv.slice(2)
/** Values consumed by a flag, so `--out /tmp/x` does not also read as a route.
 *  It did on the first run: the output directory was screenshotted as a page. */
const consumed = new Set()
const flag = (name, fallback = null) => {
  const at = args.indexOf(`--${name}`)
  if (at === -1) return fallback
  consumed.add(at + 1)
  return args[at + 1]
}
const has = (name) => args.includes(`--${name}`)
const outDir = flag('out', 'screenshots')
const theme = flag('theme', 'light')
const fullPage = has('full')
// 视口宽度。界面随根字号缩放（1920 起放大，2560 到 1.25 倍），2K 屏的版式要用
// `--width 2560 --height 1300` 单独看一遍。
const width = Number(flag('width', 1512))
const height = Number(flag('height', 1000))
const paths = args.filter((a, index) => a.startsWith('/') && !consumed.has(index))

if (!PASSWORD) {
  console.error('需要 SHOT_PASSWORD：\n  SHOT_PASSWORD=… npm run shots')
  process.exit(2)
}
const executablePath = findChromium()
if (!executablePath) {
  console.error('找不到 Chromium。设 PLAYWRIGHT_CHROMIUM=/path/to/chrome，'
    + '或 npx playwright install chromium。')
  process.exit(2)
}

let chromium
try {
  ({ chromium } = await import('playwright-core'))
} catch {
  console.error('缺少 playwright-core：npm i -D playwright-core')
  process.exit(2)
}

mkdirSync(outDir, { recursive: true })
const problems = []
const browser = await chromium.launch({ executablePath, args: ['--no-sandbox'] })
const context = await browser.newContext({
  viewport: { width, height },
  deviceScaleFactor: 1.5,
})
const page = await context.newPage()
page.on('console', (m) => {
  if (m.type() === 'error') problems.push(`控制台报错 ${page.url()}：${m.text().slice(0, 200)}`)
})
page.on('pageerror', (e) => problems.push(`未捕获异常 ${page.url()}：${String(e).slice(0, 200)}`))

await page.goto(`${BASE}/login`)
await page.fill('input[autocomplete="username"]', USER)
await page.fill('input[type="password"]', PASSWORD)
await page.click('button.submit')
await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 20000 })
// 主题存在 localStorage 里，登录之后设置一次即可，之后每页都跟着它。
await page.evaluate((value) => {
  localStorage.setItem('theme-choice', value)
  document.documentElement.setAttribute('data-theme', value)
}, theme)

let targets = paths
if (!targets.length) {
  const detail = await page.evaluate(async (base) => {
    const grab = async (path, key) => {
      const body = await (await fetch(`${base}${path}`)).json()
      return body.items?.[0]?.[key]
    }
    return [await grab('/api/events?limit=1', 'event_id'),
            await grab('/api/issues?limit=1', 'issue_id'),
            await grab('/api/risks?limit=1', 'alert_id'),
            // 公司档案同理要一个现取的 id；公司是谁只从配置来，经 /api/health 下发。
            (await (await fetch(`${base}/api/health`)).json()).companies?.[0]?.company_id]
  }, BASE)
  targets = [...ROUTES]
  if (detail[0]) targets.push(`/events/${detail[0]}`)
  if (detail[1]) targets.push(`/issues/${detail[1]}`)
  // 研判档案是这套界面上信息最密的一页，却是最后一个被加进来的：默认清单里
  // 只有带外壳的静态路由，而它要一个现取的 id。它没被截到的那几轮里，页头右侧
  // 空掉的那一大片没有任何机器检查会报。
  if (detail[2]) targets.push(`/risks/${detail[2]}`)
  if (detail[3]) targets.push(`/companies/${detail[3]}`)
}

for (const path of targets) {
  await page.goto(`${BASE}${path}`)
  await page.waitForTimeout(2600)
  const name = `${theme}${path.replace(/[^\w]/g, '_').replace(/_+/g, '_').slice(0, 48)}`
  await page.screenshot({ path: join(outDir, `${name}.png`), fullPage })
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1)
  if (overflow) problems.push(`横向溢出 ${path}`)
  console.log(`${overflow ? '!' : ' '} ${path}`)
}
await browser.close()

console.log(`\n${targets.length} 页 · 主题 ${theme} · 输出 ${outDir}/`)
if (problems.length) {
  console.error(`\n发现 ${problems.length} 处问题：\n` + problems.join('\n'))
  process.exit(1)
}
console.log('无控制台报错，无横向溢出。')
