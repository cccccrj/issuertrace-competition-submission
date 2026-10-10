<script setup>
import { computed } from 'vue'
import { LEVEL_META } from '../api.js'
import { RISK_LEVEL_GLOSS } from '../glossary.js'

const props = defineProps({
  level: { type: String, default: null },
  score: { type: Number, default: null },
})

const meta = computed(() => LEVEL_META[props.level] || { label: props.level || '—', color: 'var(--text-muted)' })
const hint = computed(() => RISK_LEVEL_GLOSS[props.level] || '')
</script>

<template>
  <!-- The score is the label's evidence, and the bar is redundant with both, so
       colour is never the only thing separating a high alert from a low one. -->
  <span class="badge nowrap" :class="{ glossed: hint }" :title="hint">
    <span class="pip" aria-hidden="true" :style="{ background: meta.color }"></span>
    <span class="label">{{ meta.label }}</span>
    <span v-if="score !== null" class="mono score">{{ score.toFixed(0) }}</span>
  </span>
</template>

<style scoped>
.badge { display: inline-flex; align-items: baseline; gap: 5px; font-size: var(--fs-aux); }
.pip { width: 8px; height: 8px; border-radius: 2px; align-self: center; flex: none; }
.label { color: var(--text-primary); font-weight: 550; }
/* 分数此前是 --fs-aux(12px)——比解释它的那句摘要（--fs-body 13px）还小，
   而 styles.css 自己写着「比它所解释的内容小一号」，方向正好反了。 */
.score { color: var(--text-secondary); font-size: var(--fs-result); font-weight: 600;
  letter-spacing: -0.01em; }
</style>
