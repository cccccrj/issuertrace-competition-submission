<script setup>
import { computed, ref } from 'vue'
import { EDGE_KIND_LABELS, edgeKindLabel, formatDate } from '../api.js'

const props = defineProps({
  members: { type: Array, required: true },
  edges: { type: Array, required: true },
})

// A chain that ran for a year can hold a hundred near-identical filings. Drawing
// all of them produces a wall, not a picture, so each depth column keeps its
// earliest few and folds the rest into one counted node.
const PER_COLUMN = 7
const COLUMN_WIDTH = 224
const ROW_HEIGHT = 48
const NODE_WIDTH = 188
const NODE_HEIGHT = 34

// Copying is what actually spreads a story, so it is the only relation drawn
// solid; everything else is dashed and labelled. Line style carries the meaning
// because a reader who cannot separate two hues must still read the graph.
const SOLID_KINDS = new Set(['REPOST', 'REWRITE', 'RECURRING'])
const DASH = {
  FROM_DISCLOSURE: '2 3', FOLLOW_UP: '6 4', RESPONSE: '6 4',
  CLARIFICATION: '1 4', CORRECTION: '1 4', CONFLICT: '8 3 2 3',
}

const hovered = ref(null)

const layout = computed(() => {
  const columns = new Map()
  for (const member of props.members) {
    const depth = member.depth ?? 0
    if (!columns.has(depth)) columns.set(depth, [])
    columns.get(depth).push(member)
  }
  const depths = [...columns.keys()].sort((a, b) => a - b)
  const placed = new Map()
  const nodes = []
  const folded = []
  depths.forEach((depth, column) => {
    const group = columns.get(depth)
      .slice()
      .sort((a, b) => String(a.published_at || '').localeCompare(String(b.published_at || '')))
    const shown = group.length > PER_COLUMN ? group.slice(0, PER_COLUMN - 1) : group
    shown.forEach((member, row) => {
      const node = {
        ...member,
        x: 16 + column * COLUMN_WIDTH,
        y: 34 + row * ROW_HEIGHT,
      }
      placed.set(member.version_id, node)
      nodes.push(node)
    })
    if (group.length > shown.length) {
      folded.push({
        key: `fold-${depth}`,
        count: group.length - shown.length,
        x: 16 + column * COLUMN_WIDTH,
        y: 34 + shown.length * ROW_HEIGHT,
      })
    }
  })
  const rows = Math.max(...depths.map((depth) => Math.min(columns.get(depth).length, PER_COLUMN)), 1)
  const drawn = props.edges
    .filter((edge) => placed.has(edge.from_version_id) && placed.has(edge.to_version_id))
    .map((edge) => {
      const from = placed.get(edge.from_version_id)
      const to = placed.get(edge.to_version_id)
      const x1 = from.x + NODE_WIDTH
      const y1 = from.y + NODE_HEIGHT / 2
      const x2 = to.x
      const y2 = to.y + NODE_HEIGHT / 2
      const bend = Math.max(24, (x2 - x1) / 2)
      return {
        ...edge,
        path: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
        labelX: (x1 + x2) / 2,
        labelY: (y1 + y2) / 2 - 5,
        solid: SOLID_KINDS.has(edge.edge_kind),
      }
    })
  return {
    nodes,
    folded,
    edges: drawn,
    width: 32 + Math.max(depths.length, 1) * COLUMN_WIDTH,
    height: 44 + rows * ROW_HEIGHT,
    // Every kind actually drawn, for the legend.
    kinds: [...new Set(drawn.map((edge) => edge.edge_kind))],
  }
})

const description = computed(() =>
  `传播关系图：${props.members.length} 个版本，${layout.value.edges.length} 条关系边，`
  + `类型包括 ${layout.value.kinds.map(edgeKindLabel).join('、') || '无'}`)
</script>

<template>
  <div class="wrap">
    <svg :viewBox="`0 0 ${layout.width} ${layout.height}`" :height="layout.height"
         :style="{ height: `${layout.height / 16}rem` }"
         role="img" :aria-label="description">
      <g>
        <path v-for="(edge, index) in layout.edges" :key="index" :d="edge.path"
              class="edge" :class="{ copy: edge.solid, dim: hovered && hovered !== edge.from_version_id
                && hovered !== edge.to_version_id }"
              :stroke-dasharray="edge.solid ? undefined : (DASH[edge.edge_kind] || '4 4')" />
      </g>
      <g>
        <text v-for="(edge, index) in layout.edges" :key="`l-${index}`" class="edge-label"
              :x="edge.labelX" :y="edge.labelY" text-anchor="middle">
          {{ edgeKindLabel(edge.edge_kind) }}<template v-if="edge.lag_hours != null">
            · {{ edge.lag_hours < 48 ? `${edge.lag_hours.toFixed(0)}小时`
                 : `${(edge.lag_hours / 24).toFixed(0)}天` }}</template>
        </text>
      </g>
      <g v-for="node in layout.nodes" :key="node.version_id" class="node"
         :class="{ dim: hovered && hovered !== node.version_id }"
         @mouseenter="hovered = node.version_id" @mouseleave="hovered = null">
        <title>{{ node.title }} · {{ node.source_name || '—' }} · {{ formatDate(node.published_at) }}</title>
        <rect :x="node.x" :y="node.y" :width="NODE_WIDTH" :height="NODE_HEIGHT" rx="5" />
        <text :x="node.x + 9" :y="node.y + 14" class="node-title">
          {{ (node.title || '（无标题）').slice(0, 17) }}
        </text>
        <text :x="node.x + 9" :y="node.y + 27" class="node-meta">
          {{ node.source_name || '—' }} · {{ formatDate(node.published_at) }} · 深度 {{ node.depth }}
        </text>
      </g>
      <g v-for="fold in layout.folded" :key="fold.key" class="fold">
        <rect :x="fold.x" :y="fold.y" :width="NODE_WIDTH" :height="NODE_HEIGHT" rx="5" />
        <text :x="fold.x + 9" :y="fold.y + 21" class="node-meta">
          另有 {{ fold.count }} 个同层版本未展开
        </text>
      </g>
    </svg>

    <!-- The legend names every relation drawn, so the picture is never read by
         line style alone either. -->
    <div class="legend">
      <span v-for="kind in layout.kinds" :key="kind" class="item">
        <svg width="26" height="8" aria-hidden="true">
          <line x1="1" y1="4" x2="25" y2="4" class="edge"
                :class="{ copy: SOLID_KINDS.has(kind) }"
                :stroke-dasharray="SOLID_KINDS.has(kind) ? undefined : (DASH[kind] || '4 4')" />
        </svg>
        {{ EDGE_KIND_LABELS[kind] || kind }}
      </span>
      <span class="item muted">实线＝复制关系（并入同一链路），虚线＝仅作旁证</span>
    </div>
  </div>
</template>

<style scoped>
.wrap { overflow-x: auto; }
svg { display: block; min-width: 100%; }
.edge { fill: none; stroke: var(--baseline); stroke-width: 1.5; }
.edge.copy { stroke: var(--series-1); stroke-width: 2; }
.edge.dim { opacity: 0.28; }
/* SVG 内的字号是 viewBox 的用户单位：图整体已按 rem 高度缩放，这里再换 rem
   就放大两次、撑出节点框。 */
.edge-label { /* keep-px */ font-size: 9.5px; fill: var(--text-muted); }
.node rect { fill: var(--surface-1); stroke: var(--border); stroke-width: 1; }
.node { cursor: default; }
.node.dim { opacity: 0.42; }
.node-title { /* keep-px */ font-size: 11px; fill: var(--text-primary); }
.node-meta { /* keep-px */ font-size: 9.5px; fill: var(--text-muted); }
.fold rect { fill: none; stroke: var(--gridline); stroke-width: 1; stroke-dasharray: 3 3; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; font-size: var(--fs-note);
  color: var(--text-secondary); align-items: center; }
.item { display: inline-flex; align-items: center; gap: 6px; }
</style>
