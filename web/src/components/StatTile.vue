<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [Number, String], required: true },
  note: { type: String, default: null },
  /** Where the number came from. A figure a reader cannot open is a figure they
   *  have to take on trust, which is the opposite of what this platform is for. */
  to: { type: [String, Object], default: null },
})
</script>

<template>
  <!-- A single number does not need a plot; a tile states it and its caveat. -->
  <component :is="to ? 'RouterLink' : 'div'" :to="to" class="tile" :class="{ linked: to }">
    <div class="tile-label">{{ label }}</div>
    <div class="tile-value mono">{{ typeof value === 'number' ? value.toLocaleString() : value }}</div>
    <div v-if="note" class="tile-note">{{ note }}</div>
  </component>
</template>

<style scoped>
.tile {
  background: var(--surface-1);
  border: 1px solid var(--border);
  /* 此前是字面 10px，而 .card 是 8px：两种圆角同屏，且是无意的。 */
  border-radius: var(--radius);
  padding: 14px 16px;
}
.tile-label { font-size: var(--fs-aux); color: var(--text-secondary); }
.tile-value { font-size: var(--fs-display); font-weight: 620; letter-spacing: -0.02em; line-height: 1.25; }
.tile-note { font-size: var(--fs-note); color: var(--text-muted); }
.linked { display: block; color: inherit; }
.linked:hover { text-decoration: none; border-color: var(--series-1); }
</style>
