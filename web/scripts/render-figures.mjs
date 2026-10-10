/**
 * 把 docs/figures/*.svg 渲染成 3840×2160 的 PNG（2 倍分辨率），给 PPT 直接用。
 *
 *   python3 docs/figures/build_figures.py
 *   cd web && node scripts/render-figures.mjs
 *
 * 需要一个 Chromium，查找顺序与 scripts/screenshots.mjs 相同：PLAYWRIGHT_CHROMIUM / CHROME
 * 环境变量 → playwright 缓存目录 → 系统 chromium。
 */
import { existsSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { chromium } from 'playwright-core'

const FIGURES = resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/figures')

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
  for (const path of ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']) {
    if (existsSync(path)) return path
  }
  return null
}

const executablePath = findChromium()
if (!executablePath) {
  console.error('找不到 Chromium：设置 PLAYWRIGHT_CHROMIUM 指向一个 chrome 可执行文件')
  process.exit(1)
}
const browser = await chromium.launch({ executablePath })
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 })
const svgs = readdirSync(FIGURES).filter((name) => name.endsWith('.svg')).sort()
for (const name of svgs) {
  await page.goto(pathToFileURL(join(FIGURES, name)).href)
  const out = join(FIGURES, name.replace(/\.svg$/, '.png'))
  await page.screenshot({ path: out, clip: { x: 0, y: 0, width: 1920, height: 1080 } })
  console.log('rendered', out)
}
await browser.close()
