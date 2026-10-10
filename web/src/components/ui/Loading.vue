<script setup>
/** The one place that decides what "still loading" looks like.
 *
 *  It had eight copies: a page-level card in `AsyncSection`, a muted line in
 *  each of the six drawers that fetch their own detail, and a placeholder
 *  inside `EChart`. They were the same words with three different wrappers, so
 *  changing the affordance — to a skeleton, to a delay before it appears —
 *  meant finding all eight.
 *
 *  `variant` is the wrapper, not the message: `card` for a panel that owns the
 *  page's main area, `inline` for a line inside something already drawn, `bare`
 *  for a slot whose parent supplies the box.
 */
defineProps({
  variant: {
    type: String,
    default: 'card',
    validator: (value) => ['card', 'inline', 'bare'].includes(value),
  },
})
</script>

<template>
  <div :class="['loading-state', variant]" role="status">加载中…</div>
</template>

<style scoped>
.loading-state { color: var(--text-muted); }
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 16px;
}
.inline { font-size: var(--fs-aux); padding: 6px 0; }
</style>
