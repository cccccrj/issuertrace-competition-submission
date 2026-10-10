<script setup>
/* 数值核对：主张里的每个数字换算成最小单位后与证据逐个比对。换算与差值由程序
   精确计算（I-02：模型曾把 708.15 亿股算成 7,081,500,000 股，报出「相差超 637 亿股」）。
   与模型结论相左时醒目标出，但不改写裁决——裁决对不对，由复核的人决定。 */
import { computed } from 'vue'

const props = defineProps({ check: { type: Object, default: null } })

const STATUS = {
  agree: { icon: '=', text: '在主张的表述精度内一致' },
  slip: { icon: '×10ⁿ', text: '相差整 10 的幂，疑似单位换算错误' },
}
const SUMMARY = {
  agree: '主张中的数字全部与证据一致',
  partial: '找到的数字一致，另有数字证据中没有',
  slip: '有数字与证据相差整 10 的幂，疑似单位换算错误',
}
const DISPUTES = {
  figures_agree: '数字在表述精度内一致，系统研判却判为「数值偏差」。',
  unit_slip: '有数字与证据恰好相差 10 的整数次幂（可能是单位写错，也可能只是巧合，比如恰好占 10%），系统研判判为「支持」。',
}
const figures = computed(() => props.check?.figures || [])
</script>

<template>
  <div v-if="check && figures.length" class="numeric" :class="{ disputed: check.disputed }">
    <div class="head">
      <b>数值核对</b>（程序换算）：{{ SUMMARY[check.status] || check.status }}
    </div>
    <p v-if="check.disputed" class="alert" role="note">
      ⚠ 数值核对与系统研判结论不符：{{ DISPUTES[check.dispute] || DISPUTES.figures_agree }}请以人工判断为准。
    </p>
    <ul>
      <li v-for="(item, index) in figures" :key="index" :class="item.status">
        <span class="mark" aria-hidden="true">{{ STATUS[item.status]?.icon }}</span>
        主张「{{ item.claim_raw }}」= <span class="mono">{{ item.claim_value }}</span>；
        证据「{{ item.evidence_raw }}」= <span class="mono">{{ item.evidence_value }}</span>；
        差 <span class="mono">{{ item.difference }}</span>（{{ item.relative }}），{{ STATUS[item.status]?.text }}
      </li>
    </ul>
    <div v-if="check.unmatched?.length" class="muted">
      证据中没有对应的数字：{{ check.unmatched.join('、') }}
    </div>
  </div>
</template>

<style scoped>
.numeric { font-size: var(--fs-aux); color: var(--text-secondary); margin: 6px 0; }
.numeric ul { margin: 4px 0 0; padding-left: 0; list-style: none; }
.numeric li { margin-bottom: 2px; overflow-wrap: anywhere; }
.mark { display: inline-block; min-width: 2.2em; font-weight: 650; color: var(--text-muted); }
li.agree .mark { color: var(--status-good); }
li.slip .mark { color: var(--status-critical); }
.alert { margin: 4px 0; padding: 6px 8px; border-left: 3px solid var(--status-warning);
  background: var(--surface-2); color: var(--text-primary); }
</style>
