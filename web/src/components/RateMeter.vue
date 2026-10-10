<script setup>
/**
 * 一张能力卡的质量率：细横条 + 百分比 + 口径。
 *
 * 比例由接口算好送来（分子、分母、口径一起），这里只画。按 dataviz 的 meter 规范：
 * 填充是强调色，轨道是同一色相浅一级；百分比和口径用文字令牌，不用系列色。
 * 分母为 0 时照实说「暂无数据」，不画一根 0% 的空条冒充结果。
 */
import { computed } from 'vue'

const props = defineProps({
  rate: { type: Object, required: true },   // { value, numerator, denominator, label, basis }
})
const percent = computed(() => (props.rate.value === null ? null : props.rate.value * 100))
</script>

<template>
  <div class="meter" :title="`${rate.basis}：${rate.numerator.toLocaleString()} / ${rate.denominator.toLocaleString()}`">
    <div class="meter-head">
      <span class="meter-label">{{ rate.label }}</span>
      <span v-if="percent !== null" class="meter-value">{{ percent.toFixed(1) }}%</span>
      <span v-else class="meter-value muted">暂无数据</span>
    </div>
    <div class="track" role="meter" :aria-label="rate.label" aria-valuemin="0" aria-valuemax="100"
         :aria-valuenow="percent === null ? undefined : Number(percent.toFixed(1))">
      <div class="fill" :style="{ width: `${percent ?? 0}%` }"></div>
    </div>
    <div class="meter-basis">{{ rate.basis }}</div>
  </div>
</template>

<style scoped>
.meter { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.meter-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.meter-label { font-size: var(--fs-note); color: var(--text-secondary); }
.meter-value { font-size: var(--fs-aux); font-weight: 600; color: var(--text-primary); }
.track {
  height: 5px; border-radius: 3px; overflow: hidden;
  background: color-mix(in srgb, var(--series-1) 16%, var(--surface-2));
}
/* 入场时从左端长到终值：宽度一开始就是终值，动的是 scaleX，不触发重排；只在首次渲染时播一次。 */
.fill {
  height: 100%; border-radius: 3px; background: var(--series-1);
  animation: meter-grow 0.8s cubic-bezier(0.2, 0.7, 0.2, 1) both;
  transform-origin: left center;
}
@keyframes meter-grow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
.meter-basis { font-size: var(--fs-note); color: var(--text-muted); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
@media (prefers-reduced-motion: reduce) {
  .fill { animation: none; }
}
</style>
