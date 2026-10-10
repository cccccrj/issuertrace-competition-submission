/** The chart rules, asserted on the option objects themselves.
 *
 *  These boot no chart and touch no DOM. That is the point of keeping the
 *  builders pure: the rules that decide whether a chart is readable — how many
 *  series, in which order, on how many axes, with a legend or without — are
 *  checkable in milliseconds, so they can be checked on every commit rather
 *  than argued about in review.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import {
  ALL_PAIRS_CAP, OVERFLOW_LABEL, SERIES, SERIES_SLOTS, assign, foldToPalette,
} from '../../charts/theme.js'
import {
  cellLabelColour, matrixHeatmap, partBar, quadrantScatter, rankedBars, sizeHistogram, stackedTrend, tierBar,
} from '../../charts/options/index.js'

const TOKENS = {
  'surface-1': '#fcfcfb', 'page': '#f9f9f7', 'text-primary': '#0b0b0b',
  'text-secondary': '#52514e', 'text-muted': '#898781', 'gridline': '#e1e0d9',
  'baseline': '#c3c2b7', 'border': 'rgba(11,11,11,.1)', 'hover-wash': 'rgba(11,11,11,.04)',
  'series-1': '#2a78d6', 'series-2': '#eb6834', 'series-3': '#1baf7a', 'series-4': '#eda100',
  'series-5': '#e87ba4', 'series-6': '#008300', 'series-7': '#4a3aa7', 'series-8': '#e34948',
  'seq-100': '#cde2fb', 'seq-250': '#86b6ef', 'seq-400': '#3987e5', 'seq-550': '#1c5cab',
  'seq-700': '#0d366b', 'div-low': '#2a78d6', 'div-mid': '#f0efec', 'div-high': '#d03b3b',
  'status-good': '#0ca30c', 'status-warning': '#fab219', 'status-serious': '#ec835a',
  'status-critical': '#d03b3b', 'status-neutral': '#898781',
}
const STATUSES = ['status-good', 'status-warning', 'status-serious', 'status-critical', 'status-neutral']
  .map((name) => TOKENS[name])

const seriesColours = (option) =>
  option.series.map((s) => s.itemStyle?.color).filter(Boolean)

describe('色板分配', () => {
  it('按固定顺序发色，不循环', () => {
    const map = assign(['a', 'b', 'c'], TOKENS)
    expect([map.a, map.b, map.c]).toEqual([TOKENS['series-1'], TOKENS['series-2'], TOKENS['series-3']])
  })

  it('第 9 个序列不生成新色，落到静默色等待被折叠', () => {
    const keys = Array.from({ length: 9 }, (_, i) => `k${i}`)
    const map = assign(keys, TOKENS)
    expect(SERIES(TOKENS)).not.toContain(map.k8)
    expect(map.k8).toBe(TOKENS['text-muted'])
  })

  it('颜色跟随实体而非名次：筛掉一个序列不会重绘幸存者', () => {
    // The stable key list is what is passed, not the filtered view.
    const all = ['a', 'b', 'c', 'd']
    const before = assign(all, TOKENS)
    const after = assign(all, TOKENS)   // 'b' hidden by a filter, keys unchanged
    expect(after.c).toBe(before.c)
    expect(after.d).toBe(before.d)
  })

  it('超过八个就折成其他，并说明折了几个', () => {
    const items = Array.from({ length: 12 }, (_, i) => ({ key: `k${i}`, label: `L${i}`, count: i + 1 }))
    const { items: folded, folded: count } = foldToPalette(items)
    expect(folded).toHaveLength(SERIES_SLOTS.length)
    expect(folded.at(-1).label).toBe(OVERFLOW_LABEL)
    expect(count).toBe(5)
    // Nothing is lost in the fold.
    expect(folded.reduce((sum, item) => sum + item.count, 0))
      .toBe(items.reduce((sum, item) => sum + item.count, 0))
  })
})

describe('单轴规则', () => {
  const built = [
    stackedTrend(TOKENS, { periods: ['1月'], series: [{ key: 'h', label: '高', colour: TOKENS['status-critical'], points: [1] }] }),
    rankedBars(TOKENS, { items: [{ label: 'a', count: 1 }] }),
    sizeHistogram(TOKENS, { buckets: [{ label: '2', count: 3 }] }),
    matrixHeatmap(TOKENS, { rows: ['a'], columns: ['b'], cells: [[0, 0, 1]] }),
  ]

  it('没有任何构造器输出第二条 y 轴', () => {
    // Two scales in one frame is the most misread chart there is; two measures
    // become two charts sharing an x axis instead.
    for (const option of built) {
      expect(Array.isArray(option.yAxis), JSON.stringify(option.yAxis)).toBe(false)
      expect(Array.isArray(option.xAxis)).toBe(false)
    }
  })
})

describe('stackedTrend', () => {
  const series = [
    { key: 'high', label: '高', colour: TOKENS['status-critical'], points: [3, 1] },
    { key: 'medium', label: '中', colour: TOKENS['status-warning'], points: [5, 2] },
  ]
  const option = stackedTrend(TOKENS, { periods: ['2026-01', '2026-02'], series })

  it('两个序列都进图例', () => {
    expect(option.legend.data).toEqual(['高', '中'])
  })

  it('分段之间留出表面色缝隙，避免相邻填充糊在一起', () => {
    expect(option.series[0].itemStyle.borderWidth).toBe(2)
  })

  it('状态色由调用方给出，不从分类槽里取', () => {
    // Risk level is a status, not an identity: it must not consume a series slot.
    for (const colour of seriesColours(option)) {
      expect(SERIES(TOKENS)).not.toContain(colour)
      expect(STATUSES).toContain(colour)
    }
  })
})

describe('rankedBars', () => {
  const option = rankedBars(TOKENS, { items: [{ label: '技术水平', count: 9 }, { label: '募资', count: 4 }] })

  it('单序列不放图例框——标题已经点明了它是什么', () => {
    expect(option.legend.show).toBe(false)
  })

  it('用单一色阶而不是分类色：长度已经承载了量级', () => {
    expect(seriesColours(option)).toEqual([TOKENS['seq-400']])
  })

  it('直接标注在条端，不必来回看坐标轴', () => {
    expect(option.series[0].label.show).toBe(true)
  })
})

describe('quadrantScatter', () => {
  const groups = ['status-critical', 'status-warning', 'status-good'].map((name, index) => ({
    key: name, label: `L${index}`, colour: TOKENS[name], points: [[0.5, 60]],
  }))

  it('三个序列以内正常构建，并画出象限参考线', () => {
    const option = quadrantScatter(TOKENS, { groups, xName: '可信度', yName: '风险分', xSplit: 0.6, ySplit: 75 })
    expect(option.series).toHaveLength(3)
    expect(option.series[0].markLine.data).toHaveLength(2)
  })

  it('超过全对上限就报错，逼调用方分面而不是再加颜色', () => {
    const four = [...groups, { key: 'x', label: 'L3', colour: TOKENS['series-4'], points: [] }]
    expect(() => quadrantScatter(TOKENS, { groups: four, xName: 'x', yName: 'y' }))
      .toThrow(new RegExp(String(ALL_PAIRS_CAP)))
  })

  it('标记点带表面色描边，重叠时仍分得开', () => {
    const option = quadrantScatter(TOKENS, { groups, xName: 'x', yName: 'y' })
    expect(option.series[0].itemStyle.borderColor).toBe(TOKENS['surface-1'])
    expect(option.series[0].symbolSize).toBeGreaterThanOrEqual(8)
  })
})

describe('matrixHeatmap', () => {
  const option = matrixHeatmap(TOKENS, {
    rows: ['支持', '冲突'], columns: ['支持', '冲突'], cells: [[0, 0, 12], [1, 1, 3]],
  })

  it('每个格子直接标数字，颜色只是第二重读法', () => {
    expect(option.series[0].label.show).toBe(true)
  })

  it('用顺序色阶，因为格子的值是量级不是身份', () => {
    expect(option.visualMap.inRange.color).toEqual(
      [TOKENS['seq-100'], TOKENS['seq-250'], TOKENS['seq-400'], TOKENS['seq-550'], TOKENS['seq-700']])
  })
})

describe('tierBar', () => {
  it('有序层级走顺序色阶，绝不占用分类槽', () => {
    // S0…S3 is a scale. Four categorical hues would claim they are four
    // unrelated kinds of thing.
    const option = tierBar(TOKENS, {
      tiers: [{ key: 'S0', label: 'S0', count: 8 }, { key: 'S1', label: 'S1', count: 10 },
              { key: 'S2', label: 'S2', count: 23 }, { key: 'S3', label: 'S3', count: 1 }],
    })
    for (const colour of seriesColours(option)) {
      expect(SERIES(TOKENS)).not.toContain(colour)
    }
  })
})

describe('partBar', () => {
  it('身份类分段按固定顺序取分类槽', () => {
    const option = partBar(TOKENS, {
      parts: [{ key: 'a', label: '甲', count: 2 }, { key: 'b', label: '乙', count: 5 }],
    })
    expect(seriesColours(option)).toEqual([TOKENS['series-1'], TOKENS['series-2']])
  })

  it('每个分段都进图例，识别不靠颜色单独完成', () => {
    const option = partBar(TOKENS, {
      parts: [{ key: 'a', label: '甲', count: 2 }, { key: 'b', label: '乙', count: 5 }],
    })
    expect(option.legend.data).toEqual(['甲', '乙'])
  })

  it('全为零时不会把坐标轴上限设成 0', () => {
    const option = partBar(TOKENS, { parts: [{ key: 'a', label: '甲', count: 0 }] })
    expect(option.xAxis.max).toBe(1)
  })
})

describe('图例必须有位置', () => {
  // ECharts centres a legend that declares none of top/bottom/left/right. That
  // is what shipped: 高/中/低 sat on top of the columns in the risk trend, and
  // 通过守门 / 文档类型排除 / 无正文 were printed across the middle of the very
  // bar they label. Neither is visible to a test that only checks series and
  // colours, so the position is asserted directly.
  const positioned = (legend) => ['top', 'bottom', 'left', 'right']
    .some((side) => legend[side] !== undefined && legend[side] !== 'auto')

  it('每个显示图例的构造器都指明了一条边', () => {
    const built = {
      stackedTrend: stackedTrend(TOKENS, {
        periods: ['2026-01'], series: [{ key: 'high', label: '高', points: [1] }],
      }),
      partBar: partBar(TOKENS, { parts: [{ key: 'a', label: '甲', count: 1 }] }),
      quadrantScatter: quadrantScatter(TOKENS, {
        groups: [{ key: 'high', label: '高', points: [[1, 2]] }],
        xName: 'x', yName: 'y', xSplit: 1, ySplit: 1,
      }),
    }
    for (const [name, option] of Object.entries(built)) {
      if (option.legend.show === false) continue
      expect(positioned(option.legend), `${name} 的图例没有位置，会被居中画进图里`).toBe(true)
    }
  })

  it('单条构成图的图例在条下方，不在条上', () => {
    // The grid here is 18px tall; a centred legend lands exactly on the stack.
    const option = partBar(TOKENS, { parts: [{ key: 'a', label: '甲', count: 1 }] })
    expect(option.legend.bottom).toBe(0)
    expect(option.grid.bottom).toBeGreaterThanOrEqual(24)
  })

  it('顶部有图例的图给它留出了高度', () => {
    const option = stackedTrend(TOKENS, {
      periods: ['2026-01'], series: [{ key: 'high', label: '高', points: [1] }],
    })
    expect(option.grid.top).toBeGreaterThan(option.legend.top ?? 0)
    expect(option.grid.top).toBeGreaterThanOrEqual(34)
  })
})

describe('用到的 ECharts 组件都注册了', () => {
  /** An unregistered component is not an error — ECharts drops the option and
   *  draws something plausible. `visualMap` was missing for as long as the
   *  heatmaps existed, so a cell of 0 and a cell of 769 were the same flat
   *  default blue: a colour channel that says the matrix is uniform is worse
   *  than no colour channel at all.
   *
   *  Read as text rather than by rendering, because the failure mode is silence
   *  — there is nothing to catch and nothing logged. */
  const read = (relative) => readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', '..', relative), 'utf8')

  /** Only the `use([...])` call counts. Checking the whole file would pass on a
   *  component that is imported and never registered — which is exactly the
   *  state the first draft of this test failed to detect when it was tried. */
  const registered = () => {
    const call = read('charts/echarts.js').match(/echarts\.use\(\[([\s\S]*?)\]\)/)
    expect(call, 'echarts.js 里找不到 use([...]) 调用').not.toBeNull()
    return call[1]
  }

  const NEEDS = {
    // option key -> component that makes it do anything
    visualMap: 'VisualMapComponent',
    tooltip: 'TooltipComponent',
    legend: 'LegendComponent',
    grid: 'GridComponent',
    markLine: 'MarkLineComponent',
    markArea: 'MarkAreaComponent',
    aria: 'AriaComponent',
  }
  const SERIES = {
    bar: 'BarChart', line: 'LineChart', scatter: 'ScatterChart',
    heatmap: 'HeatmapChart', custom: 'CustomChart',
  }

  it('每个用到的 option 键都有对应组件', () => {
    const source = read('charts/options/index.js') + read('charts/theme.js')
    const registry = registered()
    for (const [key, component] of Object.entries(NEEDS)) {
      if (!new RegExp(`\\b${key}\\s*:`).test(source)) continue
      expect(registry, `option 里用了 ${key}，但 echarts.js 没注册 ${component}`)
        .toContain(component)
    }
  })

  it('每个用到的 series type 都有对应图表', () => {
    const source = read('charts/options/index.js')
    const registry = registered()
    for (const [type, chart] of Object.entries(SERIES)) {
      if (!new RegExp(`type:\\s*'${type}'`).test(source)) continue
      expect(registry, `series 里用了 '${type}'，但 echarts.js 没注册 ${chart}`)
        .toContain(chart)
    }
  })
})

describe('混淆矩阵格内数字', () => {
  // 试用者反馈「数字都看不见了」：格内字色此前一律是 text-primary，深格上的深字、
  // 浅格上的浅字都读不出来。现在每格按底色取深字或白字，这里两套色阶各验一遍。
  const hex = (h) => [1, 3, 5].map((o) => parseInt(h.slice(o, o + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
  const lum = (h) => { const [r, g, b] = hex(h); return 0.2126 * r + 0.7152 * g + 0.0722 * b }
  const contrast = (a, b) => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05)

  for (const [name, ramp] of [['浅色', ['#cde2fb', '#86b6ef', '#3987e5', '#1c5cab', '#0d366b']],
                              ['深色', ['#cde2fb', '#86b6ef', '#3987e5', '#1c5cab', '#184f95']]]) {
    it(`${name}色阶上每一级都读得清`, () => {
      for (const stop of ramp) expect(contrast(cellLabelColour(stop), stop)).toBeGreaterThanOrEqual(4.5)
    })
  }

  it('最浅格用深字、最深格用白字', () => {
    const option = matrixHeatmap(TOKENS, { rows: ['a'], columns: ['b', 'c'], cells: [[0, 0, 0], [1, 0, 10]] })
    const colours = option.series[0].data.map((cell) => cell.label.color)
    expect(colours).toEqual(['#0b0b0b', '#ffffff'])
  })
})
