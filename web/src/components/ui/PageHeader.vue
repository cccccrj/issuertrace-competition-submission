<script setup>
/** Title, standfirst, breadcrumbs and actions — the four things every page top
 *  needs, in one place instead of sixteen.
 *
 *  The `actions` slot replaces the `float: right` the two detail pages used for
 *  their export link, which took the link out of the flow and made it collide
 *  with a long title. */
/*  `basis` 插槽是这一轮加的。页面此前把整段方法学说明常显在标题下方——不是因为
 *  写得多，是因为它横在阅读路径正中间，于是产品读起来像一份工作底稿。一句导语
 *  留在外面，其余收进折叠：一个字不删，只是不再挡路。
 *
 *  用原生 `<details>` 而不是 v-if：折叠态的文字仍在 DOM 里，十几条钉住具体句子
 *  的测试照常通过；键盘可达与展开语义也都是免费的。 */
defineProps({
  title: { type: String, required: true },
  note: { type: String, default: '' },
  /** [{ label, to }] — the trail, without the current page. */
  crumbs: { type: Array, default: () => [] },
})
</script>

<template>
  <header class="head">
    <nav v-if="crumbs.length" class="crumbs" aria-label="面包屑">
      <template v-for="(crumb, index) in crumbs" :key="crumb.to">
        <RouterLink :to="crumb.to">{{ crumb.label }}</RouterLink>
        <span v-if="index < crumbs.length - 1" aria-hidden="true">／</span>
      </template>
    </nav>
    <div class="row">
      <h1 class="page-title">{{ title }}</h1>
      <div class="actions"><slot name="actions" /></div>
    </div>
    <p v-if="note || $slots.note" class="page-note">
      <slot name="note">{{ note }}</slot>
    </p>
    <details v-if="$slots.basis" class="basis">
      <summary>口径与限制</summary>
      <div class="basis-body"><slot name="basis" /></div>
    </details>
  </header>
</template>

<style scoped>
.head { margin-bottom: 4px; }
.crumbs { font-size: var(--fs-aux); color: var(--text-secondary); margin-bottom: 6px; }
.crumbs span { margin: 0 4px; }
.row { display: flex; align-items: baseline; justify-content: space-between; gap: 28px; }
/* 事件详情页的标题是一整句摘要，会折成两行；操作链接对齐首行基线，间距不够
   时读起来像是标题的一部分。不换行、留足横向间距。 */
.page-title { min-width: 0; }
.actions { flex: none; font-size: var(--fs-body); white-space: nowrap; }
.basis { margin: calc(-1 * var(--sp-4)) 0 var(--sp-5); }
.basis summary { font-size: var(--fs-note); color: var(--text-muted); cursor: pointer;
  width: fit-content; }
.basis summary:hover { color: var(--text-secondary); }
.basis-body { font-size: var(--fs-aux); color: var(--text-secondary); max-width: var(--measure);
  margin-top: var(--sp-2); line-height: var(--lh-body); }
</style>
