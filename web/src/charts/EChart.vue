<script setup>
/**
 * The only place a chart is mounted.
 *
 * Three properties are enforced here rather than left to each call site, because
 * a discipline that depends on remembering is a discipline that erodes:
 *
 * 1. **Every chart has a sentence.** `label` is required, and it becomes the
 *    `role="img"` label. A chart nobody can describe in words is a chart whose
 *    point has not been decided.
 * 2. **Every chart ships its numbers.** The `<details>` table is always
 *    rendered, never lazily built and never optional. It is what makes the
 *    figure readable without colour vision, copyable into a report, and
 *    checkable against the database — 「输出内容可解释」 in the plainest form.
 * 3. **The option is a pure function of the tokens.** The builder receives the
 *    palette and returns an option; it never reads the DOM. That is what lets
 *    the option builders be unit-tested without booting a chart at all, and it
 *    is why a theme switch can simply re-run them.
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import Loading from '../components/ui/Loading.vue'
import echarts from './echarts.js'
import { readTokens } from './theme.js'
import { uiScale } from '../uiScale.js'

const props = defineProps({
  /** (tokens) => echarts option. Pure: no DOM, no side effects. */
  builder: { type: Function, required: true },
  /** The chart's aria-label. Required on purpose — see (1) above. */
  label: { type: String, required: true },
  height: { type: [Number, String], default: 260 },
  /** Column headers for the accessible table, e.g. ['月份', '高', '中', '低']. */
  tableColumns: { type: Array, default: () => [] },
  /** Rows for the accessible table; each an array aligned to tableColumns. */
  tableRows: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  /** Shown instead of an empty axis. A chart with nothing in it should say so
   *  in words rather than draw a bare grid the reader has to interpret. */
  empty: { type: String, default: '' },
})

const host = ref(null)
const chart = shallowRef(null)
const tokens = ref(readTokens())
const decal = ref(false)
let observer = null

const showEmpty = computed(() => !props.loading && Boolean(props.empty) && !props.tableRows.length)
const boxHeight = computed(() =>
  // rem 而不是 px：高度跟着界面缩放走，否则字号放大了、图还是那么矮。
  typeof props.height === 'number' ? `${props.height / 16}rem` : props.height)

/** Keys whose numbers are on-screen pixels of text or legend swatches.  Grid
 *  offsets are left alone: they are room around the plot, and containLabel
 *  already grows the room when the labels grow. */
const SCALED_KEYS = new Set(['fontSize', 'itemWidth', 'itemHeight', 'lineHeight'])

/** Multiply text sizes by the interface scale.  Done here, after the builder,
 *  so builders stay pure functions of the tokens and their tests keep reading
 *  the 1x numbers. */
function scaleText(value, scale) {
  if (scale === 1 || value === null || typeof value !== 'object') return value
  if (Array.isArray(value)) return value.map((item) => scaleText(item, scale))
  if (Object.getPrototypeOf(value) !== Object.prototype) return value
  const out = {}
  for (const [key, item] of Object.entries(value)) {
    out[key] = SCALED_KEYS.has(key) && typeof item === 'number'
      ? Math.round(item * scale * 10) / 10
      : scaleText(item, scale)
  }
  return out
}

function option() {
  const built = scaleText(props.builder(tokens.value), uiScale.value)
  // The texture channel rides on the option rather than the component so a
  // builder can never forget it.
  return { ...built, aria: { ...(built.aria || {}), enabled: true, decal: { show: decal.value } } }
}

function draw() {
  if (!chart.value) return
  chart.value.setOption(option(), { notMerge: true })
}

function mountChart() {
  const el = host.value
  // A zero-width container is the normal state inside a collapsed row, a hidden
  // tab, or jsdom. Initialising there produces a chart sized 0x0 that never
  // recovers, so wait for the first resize that has room in it.
  if (!el || !el.clientWidth || chart.value) return
  chart.value = echarts.init(el, null, { renderer: 'svg' })
  draw()
}

onMounted(() => {
  observer = new ResizeObserver(() => {
    if (!chart.value) mountChart()
    else chart.value.resize()
  })
  observer.observe(host.value)
  mountChart()
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart.value?.dispose()
  chart.value = null
})

watch(() => props.builder, draw)
watch(decal, draw)
watch(uiScale, draw)

// Re-read the palette after a theme flip. `flush: 'post'` for the same reason
// the component-library bridge needs it: the theme is applied by writing an
// attribute on <html>, and a computed-style read before that write is flushed
// returns the outgoing theme's colours for one frame.
watch(
  () => [document.documentElement.getAttribute('data-theme')],
  () => { tokens.value = readTokens(); draw() },
  { flush: 'post' },
)

defineExpose({ redraw: draw })
</script>

<template>
  <figure class="chart">
    <Loading v-if="loading" variant="bare" class="state" />
    <div v-else-if="showEmpty" class="state">{{ empty }}</div>
    <div
      v-show="!loading && !showEmpty"
      ref="host"
      class="canvas"
      role="img"
      :aria-label="label"
      :style="{ height: boxHeight }"
    />

    <figcaption>
      <details v-if="tableRows.length">
        <summary>以表格查看（{{ tableRows.length }} 行）</summary>
        <div class="scroll">
          <table>
            <thead>
              <tr><th v-for="column in tableColumns" :key="column">{{ column }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in tableRows" :key="index">
                <td v-for="(cell, column) in row" :key="column" :class="{ mono: typeof cell === 'number' }">
                  {{ cell }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
      <button v-if="tableRows.length" class="decal" type="button" @click="decal = !decal">
        {{ decal ? '关闭纹理' : '用纹理区分' }}
      </button>
    </figcaption>
  </figure>
</template>

<style scoped>
.chart { margin: 0; }
.canvas { width: 100%; }
.state { padding: 32px 12px; text-align: center; color: var(--text-muted); font-size: var(--fs-body); }
figcaption { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-top: 6px; }
summary { cursor: pointer; font-size: var(--fs-aux); color: var(--text-secondary); }
summary:hover { color: var(--text-primary); }
.scroll { overflow-x: auto; margin-top: 8px; }
.decal { min-height: 0; padding: 2px 8px; font-size: var(--fs-note); flex: none; }
</style>
