/** The bridge from this application's palette to the component library's theme.
 *
 *  The direction matters and is the reason this file is short: `styles.css` is
 *  the source, the library is the consumer.  Nothing here defines a colour —
 *  every value is read back out of the document by `readTokens`, so a change to
 *  the sheet reaches the library on the next theme flush and there is no second
 *  palette to keep in step.
 *
 *  Two rules travel with the mapping and are not negotiable:
 *
 *  * The status hues are mapped into the library's success/warning/error/info
 *    slots because they *are* status.  Any component rendered in one of them
 *    must still carry its Chinese label — never a bare coloured dot, bar or
 *    ring.  Colour has never been the only carrier of meaning in this interface
 *    and a library is not a reason to start.
 *  * No `--status-*` value is ever passed as a data-series colour.  The two
 *    chart-like things in the app (`VerdictBar`, `PropagationGraph`) are
 *    hand-written and stay that way.
 */
import { computed, onScopeDispose, ref, watch } from 'vue'
import { darkTheme } from 'naive-ui'

import { readTokens } from './tokens.js'
import { scalePx, uiScale } from '../uiScale.js'

const DARK_QUERY = '(prefers-color-scheme: dark)'

function buildOverrides(tokens, scale = 1) {
  const px = (value) => `${scalePx(value, scale)}px`
  return {
    common: {
      primaryColor: tokens['series-1'],
      primaryColorHover: tokens['series-1-hover'],
      primaryColorPressed: tokens['series-1-pressed'],
      primaryColorSuppl: tokens['series-1-hover'],

      successColor: tokens['status-good'],
      warningColor: tokens['status-warning'],
      errorColor: tokens['status-critical'],
      infoColor: tokens['status-neutral'],

      textColorBase: tokens['text-primary'],
      textColor1: tokens['text-primary'],
      textColor2: tokens['text-secondary'],
      textColor3: tokens['text-muted'],
      placeholderColor: tokens['text-muted'],

      baseColor: tokens['surface-1'],
      bodyColor: tokens['page'],
      cardColor: tokens['surface-1'],
      modalColor: tokens['surface-1'],
      popoverColor: tokens['surface-1'],
      inputColor: tokens['surface-1'],
      tableColor: tokens['surface-1'],
      tableHeaderColor: tokens['surface-1'],

      borderColor: tokens['border'],
      dividerColor: tokens['gridline'],
      hoverColor: tokens['hover-wash'],

      // Sized to the controls already on the page: 32px tall, 6px corners,
      // 14px text.  A library that arrives a few pixels off makes every
      // half-migrated page look broken.  Multiplied by the interface scale
      // because the page's own controls are rem after the build and grow with
      // the root; the library injects its styles at runtime, past PostCSS.
      borderRadius: px(6),
      borderRadiusSmall: px(4),
      fontSize: px(14),
      fontSizeMedium: px(14),
      heightMedium: px(32),
      heightSmall: px(28),
      fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
    },
  }
}

/**
 * Drive the library's theme from the app's own three-way setting.
 *
 * @param themeSetting a ref holding 'light' | 'dark' | 'sepia' — the same ref
 *   `App.vue` already persists to localStorage.  This observes it; it never
 *   writes to it, and the existing applyTheme logic is untouched.
 *
 *   护眼 is a light theme as far as the component library is concerned: it takes
 *   the light base and then the overrides below, which are read back out of the
 *   stylesheet, so its warm surface and softened ink reach the library's own
 *   components without a second palette being declared in JS.
 */
export function useNaiveTheme(themeSetting) {
  const media = typeof window !== 'undefined' && window.matchMedia
    ? window.matchMedia(DARK_QUERY)
    : null
  const systemDark = ref(media ? media.matches : false)

  const onSystemChange = (event) => { systemDark.value = event.matches }
  if (media) {
    media.addEventListener('change', onSystemChange)
    onScopeDispose(() => media.removeEventListener('change', onSystemChange))
  }

  // No 'auto' any more: the setting names a theme outright, and only one of
  // them is dark. `systemDark` is still watched below, because a reader whose
  // OS flips while the tab is open used to get one frame of stale tokens.
  const resolvedDark = computed(() => themeSetting.value === 'dark')

  const tokens = ref(readTokens())
  // `flush: 'post'` is load-bearing: applyTheme() writes the data-theme
  // attribute, and reading computed styles before that write has been flushed
  // returns the outgoing theme's colours for one frame.
  watch([themeSetting, systemDark], () => { tokens.value = readTokens() }, { flush: 'post' })

  return {
    theme: computed(() => (resolvedDark.value ? darkTheme : null)),
    themeOverrides: computed(() => buildOverrides(tokens.value, uiScale.value)),
    resolvedDark,
  }
}
