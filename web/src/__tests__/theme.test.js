/** The bridge from styles.css to the component library's theme.
 *
 *  The point of the bridge is that the stylesheet stays the palette's only
 *  definition.  These check that property directly: change the sheet and the
 *  theme object follows; and no status hue ever leaks into a slot that would
 *  make colour the only carrier of meaning.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { effectScope, nextTick, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { TOKEN_NAMES, readTokens } from '../theme/tokens.js'
import { useNaiveTheme } from '../theme/naive.js'

const LIGHT = {
  // 与 styles.css 的浅色块保持一致：这份 fixture 是刷进 DOM 的，留着旧值会让
  // 「每个声明的令牌都能读出值」那条悄悄退化成在验证 FALLBACK。
  'surface-0': '#e4e9f0', 'surface-2': '#e9edf4',
  'surface-1': '#ffffff', 'page': '#eef1f6', 'text-primary': '#0c1524',
  'text-secondary': '#465064', 'text-muted': '#78849a', 'gridline': '#dde3ec',
  'baseline': '#b8c1d1', 'border': 'rgba(12, 21, 36, 0.12)',
  'hover-wash': 'rgba(12, 21, 36, 0.05)', 'series-1': '#1c5cab',
  'series-1-hover': '#2a78d6', 'series-1-pressed': '#164a8c',
  'status-good': '#0ca30c', 'status-warning': '#fab219', 'status-serious': '#ec835a',
  'status-critical': '#d03b3b', 'status-neutral': '#78849a',
}

function paint(values) {
  for (const [name, value] of Object.entries(values)) {
    document.documentElement.style.setProperty(`--${name}`, value)
  }
}

let scope

function withTheme(setting) {
  scope = effectScope()
  return scope.run(() => useNaiveTheme(setting))
}

beforeEach(() => paint(LIGHT))
afterEach(() => {
  scope?.stop()
  document.documentElement.removeAttribute('style')
  document.documentElement.removeAttribute('data-theme')
})

describe('readTokens', () => {
  it('每个声明的令牌都能读出值', () => {
    const tokens = readTokens()
    for (const name of TOKEN_NAMES) expect(tokens[name], name).toBeTruthy()
  })

  it('读的是文档里的值，不是自己那份副本', () => {
    paint({ 'series-1': '#123456' })
    expect(readTokens()['series-1']).toBe('#123456')
  })

  it('令牌被删掉时回落到兜底值而不是空字符串', () => {
    document.documentElement.style.removeProperty('--status-serious')
    expect(readTokens()['status-serious']).toBeTruthy()
  })
})

describe('useNaiveTheme', () => {
  it('浅色设置下不启用深色主题', () => {
    expect(withTheme(ref('light')).theme.value).toBeNull()
  })

  it('深色设置下启用深色主题', () => {
    expect(withTheme(ref('dark')).theme.value).not.toBeNull()
  })

  it('护眼是浅色主题：库拿浅色底座，暖色由样式表里的令牌覆写', () => {
    // 「跟随系统」 is gone, so the setting names a theme outright and exactly one
    // of the three is dark. 护眼 must not resolve dark, or the library's own
    // components would go black under a cream page.
    expect(withTheme(ref('sepia')).theme.value).toBeNull()
    expect(withTheme(ref('sepia')).resolvedDark.value).toBe(false)
  })

  it('切换设置后主题跟着变', async () => {
    const setting = ref('light')
    const { theme } = withTheme(setting)
    expect(theme.value).toBeNull()
    setting.value = 'dark'
    await nextTick()
    expect(theme.value).not.toBeNull()
  })
})

describe('themeOverrides', () => {
  it('主色取自 --series-1，悬停与按下取自其派生令牌', () => {
    const { themeOverrides } = withTheme(ref('light'))
    const common = themeOverrides.value.common
    expect(common.primaryColor).toBe(LIGHT['series-1'])
    expect(common.primaryColorHover).toBe(LIGHT['series-1-hover'])
    expect(common.primaryColorPressed).toBe(LIGHT['series-1-pressed'])
  })

  it('状态色进入语义槽，且四个槽各不相同', () => {
    const { common } = withTheme(ref('light')).themeOverrides.value
    expect(common.successColor).toBe(LIGHT['status-good'])
    expect(common.warningColor).toBe(LIGHT['status-warning'])
    expect(common.errorColor).toBe(LIGHT['status-critical'])
    expect(new Set([common.successColor, common.warningColor, common.errorColor]).size).toBe(3)
  })

  it('状态色绝不作为主色使用', () => {
    // The palette reserves the status hues; a status colour standing in as the
    // accent would make a coloured control mean nothing and something at once.
    const { common } = withTheme(ref('light')).themeOverrides.value
    const statuses = ['status-good', 'status-warning', 'status-serious',
                      'status-critical', 'status-neutral'].map((name) => LIGHT[name])
    expect(statuses).not.toContain(common.primaryColor)
    expect(statuses).not.toContain(common.primaryColorHover)
  })

  it('尺寸与页面上已有的控件对齐', () => {
    // A library arriving a few pixels off makes every half-migrated page look
    // broken; these match the 32px controls and 6px corners in styles.css.
    const { common } = withTheme(ref('light')).themeOverrides.value
    expect(common.heightMedium).toBe('32px')
    expect(common.borderRadius).toBe('6px')
    expect(common.fontSize).toBe('14px')
  })

  it('改动样式表就会反映到主题里，不需要改这个文件', async () => {
    const setting = ref('light')
    const { themeOverrides } = withTheme(setting)
    expect(themeOverrides.value.common.primaryColor).toBe('#1c5cab')
    paint({ 'series-1': '#00ff00' })
    setting.value = 'dark'
    await nextTick()
    expect(themeOverrides.value.common.primaryColor).toBe('#00ff00')
  })
})

/** 表面层级：把「颜色区分再明显一点」变成一个数。
 *
 *  有人反馈四个区分不开。量下来这话有实据：改之前 .sidebar 与 .card 取的是同一个
 *  --surface-1（1.000:1，字面同色），而卡面与页底差 3/255（1.027:1）。事件详情页
 *  七个区、侧栏、页底是同一张白，只有一条 10% 的发丝线在分。
 *
 *  这条测试直接读 styles.css 的文本而不是 readTokens()：jsdom 不加载样式表，
 *  DOM 里读回的是测试自己刷上去的值，那样只能验证测试自己。conventions.test.js
 *  读源码是同一个理由。
 *
 *  阈值不是无障碍标准（那管的是文字对背景）。1.00 是同色；两张纸要能被看成两张，
 *  经验值在 1.05–1.10 之间。相邻取 1.06，导航对卡面取 1.13——那是全站跨度最大的
 *  一对，也是此前唯一完全同色的一对。
 */
describe('四级表面在三套主题下都分得开', () => {
  const sheet = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', 'styles.css'), 'utf8')

  /** 每套主题各自的令牌块。深色有两份（媒体查询兜底与显式），取显式那份。 */
  const BLOCKS = {
    浅色: /^:root \{([\s\S]*?)^\}/m,
    深色: /^:root\[data-theme='dark'\] \{([\s\S]*?)^\}/m,
    护眼: /^:root\[data-theme='sepia'\] \{([\s\S]*?)^\}/m,
  }

  const surfaces = (theme) => {
    const block = sheet.match(BLOCKS[theme])
    expect(block, `${theme} 的令牌块不见了`).not.toBeNull()
    const read = (name) => {
      const hit = block[1].match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, 'i'))
      expect(hit, `${theme} 没有定义 --${name}`).not.toBeNull()
      return hit[1]
    }
    return { nav: read('surface-0'), page: read('page'),
             card: read('surface-1'), inset: read('surface-2') }
  }

  const luminance = (hex) => {
    const channels = [1, 3, 5].map((at) => parseInt(hex.slice(at, at + 2), 16) / 255)
      .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
  }
  const ratio = (a, b) => {
    const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x)
    return (high + 0.05) / (low + 0.05)
  }

  for (const theme of Object.keys(BLOCKS)) {
    it(`${theme}：相邻两级 ≥ 1.06`, () => {
      const s = surfaces(theme)
      for (const [name, a, b] of [
        ['卡面对地面', s.card, s.page],
        ['内嵌对卡面', s.inset, s.card],
      ]) {
        expect(ratio(a, b), `${theme} ${name} 只有 ${ratio(a, b).toFixed(3)}:1`)
          .toBeGreaterThanOrEqual(1.06)
      }
    })

    it(`${theme}：导航导轨与卡面 ≥ 1.13`, () => {
      // 改之前这一对是 1.000:1——.sidebar 和 .card 取的是同一个令牌。
      const s = surfaces(theme)
      expect(ratio(s.nav, s.card), `${theme} 导航对卡面只有 ${ratio(s.nav, s.card).toFixed(3)}:1`)
        .toBeGreaterThanOrEqual(1.13)
    })
  }

  it('侧栏与卡片不再取同一个令牌', () => {
    // 这条钉的是原因而不是结果：即便四个值都达标，只要 .sidebar 又被写回
    // --surface-1，同色就会原样回来。
    const rule = sheet.match(/\.sidebar\s*\{[^}]*\}/)
    expect(rule[0]).toMatch(/background:\s*var\(--surface-0\)/)
  })
})
