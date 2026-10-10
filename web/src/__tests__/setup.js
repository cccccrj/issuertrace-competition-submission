/** Test setup: make the failures that normally scroll past into failures.
 *
 *  A Vue warning — a missing required prop, an unresolved component, a bad
 *  event name — prints to the console and the test still passes.  During a
 *  rewrite that is exactly the class of mistake being made, so warnings throw
 *  here and the suite stops on them.
 */
import { config } from '@vue/test-utils'
import { afterEach, beforeEach, expect } from 'vitest'

import { setCompanyLabels } from '../api.js'

const seen = []

config.global.config = config.global.config || {}
config.global.config.warnHandler = (message, _instance, trace) => {
  seen.push(`${message}${trace ? `\n${trace}` : ''}`)
}

// 产品代码里不许写公司名（公司名经 /api/health 从配置来）；测试夹具里可以——
// 大量用例断言页面上印着「长鑫科技」，这里替它们把那次加载做掉。
beforeEach(() => {
  seen.length = 0
  setCompanyLabels([{ company_id: 'cxmt', name: '长鑫科技' },
                    { company_id: 'wuxi_apptec', name: '药明康德' }])
})

afterEach(() => {
  if (seen.length) {
    const collected = seen.join('\n---\n')
    seen.length = 0
    expect.fail(`Vue 在渲染期间发出告警，视为失败：\n${collected}`)
  }
})

// jsdom implements neither of these, and both are read during theme resolution
// and by library components that measure themselves.
if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
  })
}

// jsdom implements no scrolling at all. Real browsers do, so a missing
// scrollIntoView is a test-environment gap rather than a defect — but left
// unstubbed it throws on every keyboard navigation and buries real failures.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}

if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}
