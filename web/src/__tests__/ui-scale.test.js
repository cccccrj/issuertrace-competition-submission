import { describe, expect, it } from 'vitest'

import { toRem } from '../../postcss-px-to-rem.js'
import { scalePx } from '../uiScale.js'

describe('界面缩放：构建期 px → rem', () => {
  it('16px 基准下换算逐像素相等', () => {
    expect(toRem('13px')).toBe('0.8125rem')
    expect(toRem('28px 28px 60px')).toBe('1.75rem 1.75rem 3.75rem')
    expect(toRem('-4px')).toBe('-0.25rem')
  })

  it('发丝线不换：1px 在 1.25 倍下会糊成两条', () => {
    expect(toRem('1px solid var(--border)')).toBe('1px solid var(--border)')
    expect(toRem('0 0 0 1.5px red')).toBe('0 0 0 1.5px red')
  })

  it('不误伤非 px 的值', () => {
    expect(toRem('calc(100vh - 2 * var(--sp-3))')).toBe('calc(100vh - 2 * var(--sp-3))')
    expect(toRem('42em')).toBe('42em')
  })

  it('JS 侧的像素按倍数取整', () => {
    expect(scalePx(14, 1)).toBe(14)
    expect(scalePx(14, 1.25)).toBe(18)
    expect(scalePx(32, 1.25)).toBe(40)
  })
})
