<script setup>
import { computed } from 'vue'
import { PARTIAL_SUBTYPE_LABELS, VERDICT_META } from '../api.js'
import { verdictGloss } from '../glossary.js'

const props = defineProps({
  verdict: { type: String, default: null },
  subtype: { type: String, default: null },
})

const meta = computed(() => VERDICT_META[props.verdict] || { label: '待裁决', icon: '·', color: 'var(--text-muted)' })
const subtypeLabel = computed(() => PARTIAL_SUBTYPE_LABELS[props.subtype] || props.subtype)
/* 悬停给大白话。「部分支持 · 时间错位」这种，光看标签猜不到是哪种出入。
   悬停够不着触屏与键盘，所以 /guide 的名词表里同样查得到——同一份文本。 */
const hint = computed(() => verdictGloss(props.verdict, props.subtype))
</script>

<template>
  <!-- Icon plus label, always: a status colour never carries the meaning alone. -->
  <span class="badge nowrap" :class="{ glossed: hint }" :title="hint">
    <span class="dot" aria-hidden="true" :style="{ color: meta.color }">{{ meta.icon }}</span>
    <span class="label">{{ meta.label }}</span>
    <span v-if="subtype" class="subtype">· {{ subtypeLabel }}</span>
  </span>
</template>

<style scoped>
.badge { display: inline-flex; align-items: baseline; gap: 4px; font-size: var(--fs-aux); }
.dot { font-size: var(--fs-aux); line-height: 1; }
.label { color: var(--text-primary); font-weight: 550; }
.subtype { color: var(--text-muted); font-size: var(--fs-note); }
</style>
