/** Chart colour, taken from the stylesheet and handed out by the job it does.
 *
 *  `src/styles.css` remains the palette's only definition; this reads it back
 *  through `theme/tokens.js` exactly as the component-library bridge does.
 *  Nothing here defines a colour.
 *
 *  Four jobs, four rules, and mixing them is the most common way a chart goes
 *  wrong:
 *
 *  * **Identity** — which source, which issue, which element type.  Categorical
 *    slots 1-8, handed out in fixed order.
 *  * **Magnitude on an ordered scale** — a source tier, a funnel depth, a
 *    heatmap cell.  The single-hue sequential ramp, never categorical slots:
 *    eight hues for an ordered quantity says "these are eight kinds", which is
 *    a different and false claim.
 *  * **Polarity** — above/below a baseline.  The diverging pair, with a neutral
 *    (not a hue) at the midpoint so "no change" looks like nothing.
 *  * **Status** — a risk level, a verdict, a delivery state.  The reserved
 *    `--status-*` set, which is never a series colour, and which always ships
 *    beside an icon and a Chinese label so the colour is never doing the work
 *    alone.  This module deliberately offers no helper that returns a status
 *    colour for a *series*, because the shape of the API is how the rule is
 *    enforced.
 */
import { readTokens } from '../theme/tokens.js'

/** Categorical slots, in the order they must be handed out. */
export const SERIES_SLOTS = [
  'series-1', 'series-2', 'series-3', 'series-4',
  'series-5', 'series-6', 'series-7', 'series-8',
]

/** Beyond this many series on an all-pairs form (scatter, bubble, small
 *  multiples) the palette can no longer keep every pair apart — only the first
 *  three validate against every other. Past it, facet instead of adding a hue. */
export const ALL_PAIRS_CAP = 3

/** The label a ninth-and-beyond series folds into. Never a generated hue. */
export const OVERFLOW_KEY = '__other__'
export const OVERFLOW_LABEL = '其他'

export const SERIES = (tokens) => SERIES_SLOTS.map((name) => tokens[name])

export const SEQUENTIAL = (tokens) =>
  ['seq-100', 'seq-250', 'seq-400', 'seq-550', 'seq-700'].map((name) => tokens[name])

/** The sequential ramp for *discrete ordered marks* (tiers, funnel stages).
 *  Its lightest step has to stay visible against the surface, so it starts one
 *  step in from the continuous ramp's floor. */
export const ORDINAL = (tokens) =>
  ['seq-250', 'seq-400', 'seq-550', 'seq-700'].map((name) => tokens[name])

export const DIVERGING = (tokens) => [tokens['div-low'], tokens['div-mid'], tokens['div-high']]

/**
 * Map a *stable* list of keys to categorical colours.
 *
 * Stable is the whole point: pass every key the chart can ever show, not the
 * filtered subset. Colour follows the entity, so hiding a series must not
 * repaint the ones that remain — a reader who has learned that orange is
 * 21财经 should not find orange means something else after a filter.
 *
 * @param keys every key the chart may render, in a fixed order
 * @returns {Object} key -> colour, plus OVERFLOW_KEY when there are more than
 *   eight; keys past the eighth map to the overflow colour and the caller is
 *   expected to have folded them into one 其他 series.
 */
export function assign(keys, tokens) {
  const palette = SERIES(tokens)
  const out = {}
  keys.forEach((key, index) => {
    out[key] = index < palette.length ? palette[index] : tokens['text-muted']
  })
  if (keys.length > palette.length) out[OVERFLOW_KEY] = tokens['text-muted']
  return out
}

/** Fold a ranked list down to what the palette can honestly show. */
export function foldToPalette(items, { limit = SERIES_SLOTS.length, value = (item) => item.count } = {}) {
  if (items.length <= limit) return { items, folded: null }
  const kept = items.slice(0, limit - 1)
  const rest = items.slice(limit - 1)
  return {
    items: [...kept, { key: OVERFLOW_KEY, label: OVERFLOW_LABEL, count: rest.reduce((sum, item) => sum + value(item), 0) }],
    folded: rest.length,
  }
}

/** Chrome shared by every chart: recessive axes, tokenised ink, no background
 *  of its own (the card behind it already has one). */
export function baseOption(tokens) {
  return {
    backgroundColor: 'transparent',
    animation: false,
    textStyle: {
      fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
      fontSize: 12,
      color: tokens['text-secondary'],
    },
    // 24 is room for the axis name, not for a legend: raising it globally to
    // clear the top-right legend squeezed the 90px cards until their y-axis
    // ticks collided into a smear. Builders that show a legend up there make
    // their own room.
    grid: { left: 8, right: 12, top: 24, bottom: 8, containLabel: true },
    tooltip: {
      backgroundColor: tokens['surface-1'],
      borderColor: tokens['border'],
      textStyle: { color: tokens['text-primary'], fontSize: 12 },
      extraCssText: 'box-shadow: 0 2px 12px rgba(0,0,0,0.10);',
    },
    legend: {
      // Text wears text tokens; the swatch beside it carries the identity.
      textStyle: { color: tokens['text-secondary'], fontSize: 12 },
      itemWidth: 10,
      itemHeight: 10,
      icon: 'roundRect',
      // A position, always. With none of top/bottom/left/right set, ECharts
      // centres the legend in the canvas — which put 高/中/低 on top of the
      // columns in the risk trend and printed 通过守门 across the middle of
      // every part bar. Builders that want it elsewhere override this; nothing
      // gets to inherit "wherever".
      top: 0,
      right: 0,
    },
    aria: {
      // The decal channel: off by default, turned on for the colour-vision,
      // print and forced-colors cases. Never decorative.
      enabled: true,
      decal: { show: false },
    },
  }
}

/** Axis defaults: the grid recedes, the baseline does not. */
export function axis(tokens, { name = undefined, type = 'category' } = {}) {
  return {
    type,
    name,
    nameTextStyle: { color: tokens['text-muted'], fontSize: 11 },
    axisLine: { lineStyle: { color: tokens['baseline'] } },
    axisTick: { show: false },
    axisLabel: { color: tokens['text-muted'], fontSize: 11 },
    splitLine: { lineStyle: { color: tokens['gridline'] } },
  }
}

export { readTokens }
