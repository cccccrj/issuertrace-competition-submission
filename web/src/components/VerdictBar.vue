<script setup>
import { computed, ref } from 'vue'
import { VERDICT_META } from '../api.js'

const props = defineProps({
  counts: { type: Object, required: true },
  total: { type: Number, required: true },
})

const hovered = ref(null)

// Fixed order, never sorted by size: the reader compares the same segment in the
// same place across companies.
const ORDER = ['SUPPORTED', 'PARTIAL', 'CONFLICT', 'UNVERIFIABLE']

const segments = computed(() =>
  ORDER.map((key) => ({
    key,
    ...VERDICT_META[key],
    count: props.counts[key] || 0,
    share: props.total ? (props.counts[key] || 0) / props.total : 0,
  })).filter((segment) => segment.count > 0),
)

const percent = (share) => `${(share * 100).toFixed(1)}%`
</script>

<template>
  <div>
    <div class="bar" role="img" :aria-label="`裁决分布：${segments.map((s) => `${s.label} ${s.count}`).join('，')}`">
      <div
        v-for="segment in segments"
        :key="segment.key"
        class="segment"
        :style="{ width: percent(segment.share), background: segment.color }"
        @mouseenter="hovered = segment.key"
        @mouseleave="hovered = null"
      >
        <span v-if="hovered === segment.key" class="tip">
          {{ segment.label }} {{ segment.count.toLocaleString() }} · {{ percent(segment.share) }}
        </span>
      </div>
    </div>
    <!-- The legend is the relief for sub-3:1 status fills: every segment is
         named and counted in text, so the bar is never read by colour alone. -->
    <div class="legend">
      <span v-for="segment in segments" :key="segment.key" class="item">
        <span class="swatch" :style="{ background: segment.color }" aria-hidden="true"></span>
        <span class="icon" :style="{ color: segment.color }" aria-hidden="true">{{ segment.icon }}</span>
        {{ segment.label }}
        <b class="mono">{{ segment.count.toLocaleString() }}</b>
        <span class="muted">{{ percent(segment.share) }}</span>
      </span>
    </div>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  height: 12px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--gridline);
  /* A 2px surface gap keeps adjacent fills from reading as one block. */
  gap: 2px;
}
.segment { position: relative; min-width: 3px; }
.tip {
  position: absolute;
  bottom: 18px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--text-primary);
  color: var(--surface-1);
  padding: 4px 8px;
  border-radius: 5px;
  font-size: var(--fs-note);
  white-space: nowrap;
  z-index: 5;
  pointer-events: none;
}
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-top: 10px; font-size: var(--fs-aux); color: var(--text-secondary); }
.item { display: inline-flex; align-items: center; gap: 5px; }
.swatch { width: 9px; height: 9px; border-radius: 2px; }
.icon { font-size: var(--fs-note); }
b { color: var(--text-primary); font-weight: 600; }
</style>
