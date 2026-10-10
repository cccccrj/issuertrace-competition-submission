/** Read the design tokens back out of the document.
 *
 *  `src/styles.css` is the palette's only definition — it carries the provenance
 *  ("from the validated reference palette") and the rule that status hues are
 *  never reused as a series colour.  Anything that needs those values in JS,
 *  such as a component library's theme object, reads them from here rather than
 *  restating them, because a second copy is a second thing to keep in sync and
 *  the first symptom of drift is a chart whose colours no longer match the page.
 */

/** Every token a JS consumer may ask for.  Names match the CSS custom property
 *  minus the leading dashes. */
export const TOKEN_NAMES = [
  'surface-0', 'surface-1', 'surface-2', 'page', 'text-primary', 'text-secondary', 'text-muted',
  'gridline', 'baseline', 'border', 'hover-wash',
  'series-1', 'series-1-hover', 'series-1-pressed',
  'series-2', 'series-3', 'series-4', 'series-5', 'series-6', 'series-7', 'series-8',
  'seq-100', 'seq-250', 'seq-400', 'seq-550', 'seq-700',
  'div-low', 'div-mid', 'div-high',
  'status-good', 'status-warning', 'status-serious', 'status-critical', 'status-neutral',
]

/** The light values, used only when a read comes back empty — a renamed token,
 *  or a call before the stylesheet has applied.  These are a fallback, never a
 *  source: if one is ever visibly in use, `styles.css` and this list disagree. */
const FALLBACK = {
  'surface-0': '#e4e9f0',
  'surface-1': '#ffffff',
  'surface-2': '#e9edf4',
  'page': '#eef1f6',
  'text-primary': '#0c1524',
  'text-secondary': '#465064',
  'text-muted': '#78849a',
  'gridline': '#dde3ec',
  'baseline': '#b8c1d1',
  'border': 'rgba(12, 21, 36, 0.12)',
  'hover-wash': 'rgba(12, 21, 36, 0.05)',
  'series-1': '#1c5cab',
  'series-1-hover': '#2a78d6',
  'series-1-pressed': '#164a8c',
  'series-2': '#eb6834',
  'series-3': '#1baf7a',
  'series-4': '#eda100',
  'series-5': '#e87ba4',
  'series-6': '#008300',
  'series-7': '#4a3aa7',
  'series-8': '#e34948',
  'seq-100': '#cde2fb',
  'seq-250': '#86b6ef',
  'seq-400': '#3987e5',
  'seq-550': '#1c5cab',
  'seq-700': '#0d366b',
  'div-low': '#1c5cab',
  'div-mid': '#e9edf4',
  'div-high': '#d03b3b',
  'status-good': '#0ca30c',
  'status-warning': '#fab219',
  'status-serious': '#ec835a',
  'status-critical': '#d03b3b',
  'status-neutral': '#78849a',
}

/** Current values of every token, as the browser resolves them.
 *
 *  Call after any change to `data-theme` has been flushed to the DOM — the
 *  values that come back are whichever theme block is winning at that moment.
 */
export function readTokens(element = document.documentElement) {
  const style = getComputedStyle(element)
  const tokens = {}
  for (const name of TOKEN_NAMES) {
    tokens[name] = style.getPropertyValue(`--${name}`).trim() || FALLBACK[name]
  }
  return tokens
}
