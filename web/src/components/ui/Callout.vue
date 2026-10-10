<script setup>
/** One callout, replacing three ad-hoc implementations.
 *
 *  The tone tints a 2px left border and nothing else: the message itself always
 *  carries the meaning in words, so the colour is a second reading rather than
 *  the only one. */
defineProps({
  tone: { type: String, default: 'info' },   // info | ok | warn | bad
  title: { type: String, default: '' },
})
</script>

<template>
  <div class="callout" :class="tone">
    <b v-if="title">{{ title }}</b>
    <slot />
  </div>
</template>

<style scoped>
.callout {
  border: 1px solid var(--border);
  border-left-width: 2px;
  border-radius: 8px;
  padding: 9px 12px;
  margin-bottom: 12px;
  font-size: var(--fs-body);
  background: var(--surface-1);
  color: var(--text-secondary);
}
.callout b { color: var(--text-primary); margin-right: 6px; }
.info { border-left-color: var(--series-1); }
.ok { border-left-color: var(--status-good); }
.warn { border-left-color: var(--status-warning); }
.bad { border-left-color: var(--status-critical); }
</style>
