/** Chart options as pure functions.
 *
 *  Each builder takes the palette and the data and returns an ECharts option.
 *  None of them touch the DOM, so the rules that matter — how many series, in
 *  which colours, with a legend or without, on how many axes — are unit-tested
 *  directly, without a browser and without booting a chart.
 *
 *  Two rules are enforced by the shape of these functions rather than by
 *  review:
 *
 *  * **Status is not identity.** Builders whose categories are statuses
 *    (risk level, verdict, delivery state) take their colours from the caller's
 *    explicit status map; they never reach into the categorical slots. Builders
 *    whose categories are identities take slots in fixed order via `assign`.
 *  * **One axis.** No builder here emits a second `yAxis`. Two measures of
 *    different scale become two charts sharing an x axis — never two scales in
 *    one frame, which is the single most misread chart there is.
 */
import { ALL_PAIRS_CAP, assign, axis, baseOption, ORDINAL, SEQUENTIAL } from '../theme.js'

const gap = { itemStyle: { borderColor: 'transparent', borderWidth: 2 } }

/**
 * Stacked columns over time: one column per period, split by an ordered status.
 *
 * @param periods ['2026-01', …]
 * @param series  [{ key, label, colour, points: number[] }] — colour supplied by
 *   the caller because these are statuses, not identities.
 */
export function stackedTrend(tokens, { periods, series, valueName = '数量' }) {
  return {
    ...baseOption(tokens),
    legend: { ...baseOption(tokens).legend, data: series.map((s) => s.label) },
    grid: { ...baseOption(tokens).grid, top: 38 },
    xAxis: { ...axis(tokens, { type: 'category' }), data: periods },
    yAxis: { ...axis(tokens, { type: 'value', name: valueName }) },
    series: series.map((s) => ({
      name: s.label,
      type: 'bar',
      stack: 'total',
      data: s.points,
      itemStyle: { ...gap.itemStyle, color: s.colour, borderRadius: [2, 2, 0, 0] },
    })),
  }
}

/**
 * A ranked horizontal bar: magnitude over an ordered list, one hue.
 *
 * Horizontal because the labels are Chinese phrases, which do not fit under a
 * vertical column without rotating them, and rotated labels are unreadable.
 */
export function rankedBars(tokens, { items, direct = true }) {
  const colour = SEQUENTIAL(tokens)[2]
  return {
    ...baseOption(tokens),
    legend: { show: false },   // one series: the title names it
    grid: { ...baseOption(tokens).grid, left: 8, right: 40 },
    // No axis name, and the parameter that supplied it is gone with it. The
    // name sits at the axis end, which on a horizontal bar chart is the
    // top-right corner — directly on the last tick label, so 「标准化率」 was
    // printed over 「100」. What the values are is already said twice: on each
    // bar, and in the card title.
    xAxis: { ...axis(tokens, { type: 'value' }) },
    yAxis: {
      ...axis(tokens, { type: 'category' }),
      data: items.map((item) => item.label).reverse(),
      splitLine: { show: false },
    },
    series: [{
      type: 'bar',
      data: items.map((item) => item.count).reverse(),
      itemStyle: { color: colour, borderRadius: [0, 4, 4, 0] },
      label: direct
        ? { show: true, position: 'right', color: tokens['text-secondary'], fontSize: 11 }
        : { show: false },
    }],
  }
}

/**
 * One stacked bar showing parts of a whole, with every part in the legend.
 *
 * @param parts [{ key, label, count, colour? }] — colour optional: supplied for
 *   statuses, taken from the categorical slots in fixed order otherwise.
 */
export function partBar(tokens, { parts, stableKeys = null }) {
  const palette = assign(stableKeys || parts.map((part) => part.key), tokens)
  return {
    ...baseOption(tokens),
    // Below the bar, not over it: this grid is 18px tall and a centred legend
    // lands exactly on the stack it is meant to explain.
    legend: { ...baseOption(tokens).legend, top: 'auto', right: 'auto',
              bottom: 0, left: 0, data: parts.map((part) => part.label) },
    grid: { left: 0, right: 0, top: 2, bottom: 26, containLabel: false },
    xAxis: { type: 'value', show: false, max: parts.reduce((sum, part) => sum + part.count, 0) || 1 },
    yAxis: { type: 'category', show: false, data: [''] },
    series: parts.map((part) => ({
      name: part.label,
      type: 'bar',
      stack: 'total',
      barWidth: 18,
      data: [part.count],
      itemStyle: { ...gap.itemStyle, color: part.colour || palette[part.key] },
    })),
  }
}

/** A count histogram over ordered buckets — one hue, because the buckets are an
 *  ordered scale rather than eight different things. */
export function sizeHistogram(tokens, { buckets, valueName = '数量', bucketName = '' }) {
  return {
    ...baseOption(tokens),
    legend: { show: false },
    xAxis: { ...axis(tokens, { type: 'category' }), data: buckets.map((b) => b.label), name: bucketName },
    // These sit in 90px cards. Left to itself ECharts fits ten ticks into forty
    // pixels of plot and they overlap into an unreadable smear; three is what
    // that height can carry.
    yAxis: { ...axis(tokens, { type: 'value', name: valueName }), splitNumber: 3 },
    series: [{
      type: 'bar',
      data: buckets.map((b) => b.count),
      itemStyle: { color: SEQUENTIAL(tokens)[2], borderRadius: [2, 2, 0, 0] },
    }],
  }
}

/**
 * Scatter with quadrant reference lines.
 *
 * Capped at ALL_PAIRS_CAP series: on a form where every pair of colours can
 * land side by side, only the first three slots stay apart for every kind of
 * colour vision. A fourth series is a facet, not a fourth hue.
 */
export function quadrantScatter(tokens, { groups, xName, yName, xSplit, ySplit }) {
  if (groups.length > ALL_PAIRS_CAP) {
    throw new Error(`散点最多 ${ALL_PAIRS_CAP} 个序列（收到 ${groups.length}）：超过就分面，不要再加颜色`)
  }
  return {
    ...baseOption(tokens),
    legend: { ...baseOption(tokens).legend, data: groups.map((group) => group.label) },
    grid: { ...baseOption(tokens).grid, top: 38 },
    xAxis: { ...axis(tokens, { type: 'value', name: xName }), scale: true },
    yAxis: { ...axis(tokens, { type: 'value', name: yName }), scale: true },
    series: groups.map((group, index) => ({
      name: group.label,
      type: 'scatter',
      symbolSize: 9,
      data: group.points,
      itemStyle: { color: group.colour, borderColor: tokens['surface-1'], borderWidth: 2 },
      markLine: index === 0 && (xSplit != null || ySplit != null)
        ? {
            silent: true,
            symbol: 'none',
            lineStyle: { color: tokens['baseline'], type: 'dashed' },
            label: { color: tokens['text-muted'], fontSize: 11 },
            data: [
              ...(xSplit != null ? [{ xAxis: xSplit, label: { formatter: `${xName} ${xSplit}` } }] : []),
              ...(ySplit != null ? [{ yAxis: ySplit, label: { formatter: `${yName} ${ySplit}` } }] : []),
            ],
          }
        : undefined,
    })),
  }
}

/**
 * A matrix heatmap: said × should-have for a confusion matrix, or
 * previous → next for a status flow. Sequential, because a cell's value is a
 * magnitude, and every cell is labelled with its number so the colour is a
 * second reading rather than the only one.
 */
/** Relative luminance of a `#rrggbb` colour (WCAG). */
function luminance(hex) {
  const channel = (offset) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)
}

/** The ramp colour visualMap paints at `ratio` (0–1), interpolated the same way. */
function rampAt(ramp, ratio) {
  const position = Math.min(Math.max(ratio, 0), 1) * (ramp.length - 1)
  const low = Math.floor(position)
  const high = Math.min(low + 1, ramp.length - 1)
  const mix = (offset) => Math.round(parseInt(ramp[low].slice(offset, offset + 2), 16) * (1 - (position - low))
    + parseInt(ramp[high].slice(offset, offset + 2), 16) * (position - low))
  return `#${[1, 3, 5].map((offset) => mix(offset).toString(16).padStart(2, '0')).join('')}`
}

/** 格内数字按各自的底色取深字或白字。此前一律用 text-primary：浅色主题下它是深色，
 *  压在深蓝格上看不见；深色主题下它是浅色，压在浅蓝格上看不见——试用者反馈混淆矩阵
 *  「数字都看不见了」（2026-10-08）。取对比度更高的那一个，三套主题都成立。 */
export function cellLabelColour(background) {
  if (!/^#[0-9a-f]{6}$/i.test(background || '')) return undefined
  const light = luminance(background)
  const dark = '#0b0b0b'
  const contrast = (a, b) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
  return contrast(light, luminance(dark)) >= contrast(light, 1) ? dark : '#ffffff'
}

export function matrixHeatmap(tokens, { rows, columns, cells, valueName = '数量' }) {
  const max = cells.reduce((peak, cell) => Math.max(peak, cell[2]), 0)
  const ramp = SEQUENTIAL(tokens)
  return {
    ...baseOption(tokens),
    legend: { show: false },
    grid: { ...baseOption(tokens).grid, left: 8, right: 8, top: 28, bottom: 8, containLabel: true },
    xAxis: { ...axis(tokens, { type: 'category' }), data: columns, splitLine: { show: false } },
    yAxis: { ...axis(tokens, { type: 'category' }), data: rows, splitLine: { show: false } },
    visualMap: {
      show: false,
      min: 0,
      max: max || 1,
      inRange: { color: SEQUENTIAL(tokens) },
    },
    series: [{
      type: 'heatmap',
      data: cells.map((cell) => {
        const colour = cellLabelColour(rampAt(ramp, max ? cell[2] / max : 0))
        return colour ? { value: cell, label: { color: colour } } : cell
      }),
      label: { show: true, color: tokens['text-primary'], fontSize: 11 },
      itemStyle: { borderColor: tokens['surface-1'], borderWidth: 2 },
      name: valueName,
    }],
  }
}

/** Ordered tiers as one stacked bar, on the ordinal ramp rather than the
 *  categorical slots: S0…S3 is a scale, not four unrelated categories. */
export function tierBar(tokens, { tiers }) {
  const ramp = ORDINAL(tokens)
  return partBar(tokens, {
    parts: tiers.map((tier, index) => ({ ...tier, colour: ramp[Math.min(index, ramp.length - 1)] })),
  })
}
