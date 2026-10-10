<script setup>
import { computed } from 'vue'

import { magnitudeBasisLabel } from '../api.js'

// 一个分类值，加上「凭什么是这一类」——默认收起。
//
// 两条约束同时成立，答案就是折叠：一位读者说页面上解释性文字太多（2026-09-06 因此净删
// 了 2,592 字），另一位说分类的依据太笼统。所以值本身仍是唯一常显的东西，依据点开才有，
// 页面的常驻正文一个字都没有增加。用原生 <details> 而不是自造弹层：折叠态的文字仍在
// DOM 里（钉句子的测试照常过），键盘可达是免费的。
//
// reason 的质量由提示词那条「必须引用原文中支撑该判断的具体内容，不要复述判据」保证；
// 项目自己在 CausalEdgeRow 里判过「把计算过程当成理由」的死刑，这里不重犯。
const props = defineProps({
  // 展示值（已经过中文标签映射）。
  value: { type: String, default: '' },
  // 后端 /api/events/{id} 的 basis 块；null 表示这条判据还没覆盖这个事件。
  basis: { type: Object, default: null },
  // 规则判的还是模型判的（event_records.label_source），只在没有 labels 行时用。
  labelSource: { type: String, default: null },
})

const display = computed(() => props.value || '—')
const percent = computed(() => (typeof props.basis?.confidence === 'number'
  ? `${Math.round(props.basis.confidence * 100)}%`
  : null))
// basis 列记的是谁做的判定：model / propagation_edge / rule。
const decidedBy = computed(() => ({
  system: '系统研判',
  propagation_edge: '传播图判定（确定性边，优先于系统研判）',
  rule: '规则判定',
}[props.basis?.basis] || props.basis?.basis || null))
const hasReason = computed(() => Boolean(props.basis?.reason))
</script>

<template>
  <details v-if="props.basis" class="basis">
    <summary>
      <span class="value">{{ display }}</span>
      <span class="hint">依据</span>
    </summary>
    <p v-if="hasReason" class="small reason">{{ props.basis.reason }}</p>
    <p v-else class="small muted">该判定没有留下理由文本。</p>
    <p v-if="props.basis.magnitude_basis" class="small muted">
      判为重大的依据：{{ magnitudeBasisLabel(props.basis.magnitude_basis) }}
    </p>
    <p class="small muted meta">
      <span v-if="percent">置信度 {{ percent }}</span>
      <span v-if="decidedBy">{{ decidedBy }}</span>
    </p>
  </details>
  <details v-else class="basis">
    <!-- 「未判」与「判了但没给理由」必须分得开：生产方式的重分类只覆盖 1,033 / 1,907
         条事件（公告类根本不进那一轮），把两者都渲染成空白会让读者以为系统没话说。 -->
    <summary>
      <span class="value">{{ display }}</span>
      <span class="hint">依据</span>
    </summary>
    <p class="small muted">
      该判据尚未覆盖此条事件<span v-if="props.labelSource">，当前取值来自{{
        props.labelSource === 'rule' ? '规则路径' : '一次整体抽取' }}</span>。
    </p>
  </details>
</template>

<style scoped>
.basis > summary { list-style: revert; cursor: pointer; }
.basis > summary::marker { color: var(--text-muted); }
.value { color: var(--text-primary); }
.hint { color: var(--text-muted); font-size: var(--fs-aux); margin-left: 6px; }
/* 展开后的理由此前和普通段落长得一样，读者分不出「这是依据」还是「这是正文」。
   给它内嵌底标成次级材料——但不给蓝色左线：这是模型陈述的理由，不是披露原文，
   戴上原文的记号等于替它升一级。 */
.reason { margin: var(--sp-1) 0 0; background: var(--surface-2); border-radius: var(--radius-sm);
  padding: var(--sp-2) var(--sp-3); }
.meta { display: flex; gap: 10px; flex-wrap: wrap; margin: 4px 0 0; }
</style>
