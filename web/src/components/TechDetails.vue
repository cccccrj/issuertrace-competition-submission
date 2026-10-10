<script setup>
/**
 * 技术详情 —— 出处与参数，收起来。
 *
 * 有人看完平台说「技术细节折叠到『技术详情』里」。摸底之后，这些东西此前只在
 * BasisDetails 一个组件里被系统性折叠过，其余散在四类地方常显：卡片脚注
 * （「映射 v2+fa9be95d · qwen-plus · 2026-09-06」）、整张专门的表、DataTable 的
 * 列，以及元数据行。
 *
 * 它收的是**出处与参数**，不是**数字的含义**。模型名、提示词版本、数据集哈希、
 * 置信度、相似度门限、抽样与切分的机制——这些是「这个数是怎么来的」，读者问到
 * 才要。而「这一栏不是这个人的准确率」「精确率高召回低意味着什么」是数字本身的
 * 分母与单位，把它们折起来就是亲手制造 docs/界面体检记录.md §4.3 记下的那两种
 * 误读，所以它们留在外面。
 *
 * 用原生 <details>：折叠态的文字仍在 DOM 里，一片钉具体句子的测试照常通过；键盘
 * 可达与展开语义也都是免费的。默认收起——两个视图的测试断言所有 <details> 都不带
 * open 属性。
 */
defineProps({
  /** [{ label, value }] —— value 为空的条目自动略过，缺字段不留空行。 */
  items: { type: Array, default: () => [] },
  /** 摘要右侧的一句提示，例如「本次运行」。 */
  hint: { type: String, default: '' },
})
</script>

<template>
  <details class="tech">
    <summary>
      技术详情<span v-if="hint" class="hint">{{ hint }}</span>
    </summary>
    <dl v-if="items.some((item) => item.value)" class="fields">
      <template v-for="item in items.filter((entry) => entry.value)" :key="item.label">
        <dt>{{ item.label }}</dt>
        <dd class="mono">{{ item.value }}</dd>
      </template>
    </dl>
    <div class="body"><slot /></div>
  </details>
</template>

<style scoped>
.tech > summary { list-style: revert; cursor: pointer; width: fit-content;
  font-size: var(--fs-note); color: var(--text-muted); }
.tech > summary::marker { color: var(--text-muted); }
.tech > summary:hover { color: var(--text-secondary); }
.hint { margin-left: 6px; }
/* 与证据区同一个内嵌底：技术详情也是嵌在卡里的次级材料。不给蓝色左线——
   那个记号的意思是「带页码的逐字原文」。 */
.fields {
  display: grid; grid-template-columns: max-content 1fr; gap: 3px var(--sp-3);
  margin: var(--sp-2) 0 0; padding: var(--sp-2) var(--sp-3);
  background: var(--surface-2); border-radius: var(--radius-sm);
  font-size: var(--fs-aux);
}
.fields dt { color: var(--text-muted); }
.fields dd { margin: 0; color: var(--text-secondary); overflow-wrap: anywhere; }
.body { font-size: var(--fs-aux); color: var(--text-secondary); max-width: var(--measure);
  line-height: var(--lh-body); margin-top: var(--sp-2); }
.body:empty { display: none; }
</style>
