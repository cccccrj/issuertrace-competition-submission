<script setup>
import { computed } from 'vue'

import { riskPartLabel } from '../api.js'

// 风险分的七项加减，逐项列出，合计等于分数。
//
// 为什么不是堆叠条（可信度四项用的那个 partBar）：这七项里有减分——二手稿件 −10，
// 以及封顶那一项——把负数画进堆叠会把减分显示成一段贡献。逐项加减更像一张收据，
// 而收据正是这里要的东西：读者要能自己把这几项加出那个数。
const props = defineProps({
  parts: { type: Array, default: () => [] },
  score: { type: Number, default: null },
})

const total = computed(() => props.parts.reduce((sum, part) => sum + Number(part.points || 0), 0))
const signed = (points) => `${Number(points) > 0 ? '+' : ''}${Number(points).toFixed(0)}`
</script>

<template>
  <div v-if="props.parts.length" class="breakdown">
    <div v-for="part in props.parts" :key="part.key" class="row">
      <span class="label">{{ riskPartLabel(part.key) }}</span>
      <span v-if="part.detail" class="muted small detail">{{ part.detail }}</span>
      <span class="mono points" :class="{ minus: Number(part.points) < 0 }">{{ signed(part.points) }}</span>
    </div>
    <div class="row total">
      <span class="label">合计</span>
      <span class="mono points">{{ total.toFixed(0) }}</span>
    </div>
  </div>
  <p v-else class="muted small">该事件没有存下风险分明细。</p>
</template>

<style scoped>
.breakdown { display: flex; flex-direction: column; gap: 2px; }
.row { display: flex; align-items: baseline; gap: 8px; font-size: var(--fs-aux); }
.label { color: var(--text-primary); }
.detail { flex: 1; }
.points { margin-left: auto; color: var(--text-secondary); }
.points.minus { color: var(--text-primary); }
/* 合计此前只靠 font-weight 550 与七行明细区分——那是这张表的结果，其余是过程。 */
.total { border-top: 1px solid var(--border); margin-top: 3px; padding-top: 4px; font-weight: 600; }
.total .points { color: var(--text-primary); font-size: var(--fs-result); letter-spacing: -0.01em; }
</style>
