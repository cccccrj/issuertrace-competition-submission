<script setup>
/**
 * 六张能力卡上方的一条流水线：采集文档 → 事件 → 原子主张 → 同源簇 → 专题 → 未处置预警。
 *
 * 六张卡并排时长得一样重，读不出它们是**一条线上的六道工序**——这条带子把顺序画出来。
 * 每个节点的数就是对应卡片的头条数字（第一个节点是采集文档数），全部来自接口；点节点
 * 滚到对应的卡。连接线上一道缓慢流动的虚线，表示「信息在往下游走」，reduced-motion 时静止。
 */
defineProps({
  /** [{ key, label, value }]，按流水线顺序。 */
  nodes: { type: Array, required: true },
})
const emit = defineEmits(['pick'])
</script>

<template>
  <ol class="strip" aria-label="处理流水线">
    <li v-for="(node, index) in nodes" :key="node.key" class="stage">
      <button type="button" class="node" @click="emit('pick', node.key)">
        <span class="value">{{ node.value === null || node.value === undefined ? '—' : node.value.toLocaleString() }}</span>
        <span class="label">{{ node.label }}</span>
      </button>
      <svg v-if="index < nodes.length - 1" class="link" viewBox="0 0 100 8" preserveAspectRatio="none"
           aria-hidden="true">
        <line class="rail" x1="0" y1="4" x2="100" y2="4" />
        <line class="flow" x1="0" y1="4" x2="100" y2="4" />
        <path class="head" d="M94 1 L100 4 L94 7" />
      </svg>
    </li>
  </ol>
</template>

<style scoped>
.strip { list-style: none; margin: 0 0 14px; padding: 14px 16px; display: flex; align-items: center;
  border: 1px solid var(--border); border-radius: var(--radius);
  background:
    radial-gradient(120% 140% at 0% 0%, color-mix(in srgb, var(--series-1) 7%, transparent), transparent 60%),
    var(--surface-1); }
.stage { flex: 1 1 0; display: flex; align-items: center; min-width: 0; }
.stage:last-child { flex: 0 0 auto; }
.node { flex: none; display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  min-height: 0; padding: 6px 10px; border: 1px solid transparent; border-radius: 8px;
  background: none; cursor: pointer; text-align: left;
  transition: border-color 0.16s ease, background 0.16s ease, transform 0.16s ease; }
.node:hover, .node:focus-visible { border-color: var(--border); background: var(--hover-wash);
  transform: translateY(-1px); }
.value { font-size: var(--fs-result); font-weight: 640; letter-spacing: -0.01em; color: var(--text-primary); }
.label { font-size: var(--fs-note); color: var(--text-secondary); white-space: nowrap; }
.link { flex: 1 1 auto; height: 8px; min-width: 16px; margin: 0 4px; overflow: visible; }
.rail { stroke: var(--gridline); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.flow { stroke: var(--series-1); stroke-width: 1.5; stroke-dasharray: 6 14; opacity: 0.75;
  vector-effect: non-scaling-stroke; animation: strip-flow 6s linear infinite; }
.head { fill: none; stroke: var(--baseline); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
@keyframes strip-flow { to { stroke-dashoffset: -200; } }
/* 窄屏：三个一行折成两行，箭头照旧指向下一个。 */
@media (max-width: 900px) {
  .strip { flex-wrap: wrap; row-gap: 8px; }
  .stage { flex: 1 1 30%; }
  .stage:last-child { flex: 1 1 30%; }
}
@media (prefers-reduced-motion: reduce) {
  .flow { animation: none; }
  .node { transition: none; }
}
</style>
