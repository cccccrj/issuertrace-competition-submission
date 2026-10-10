<script setup>
/** One row of controls above the content, per the interaction spec.
 *
 *  Ten pages built this row by hand. The shared version adds the thing none of
 *  them had: a count of what is currently narrowing the list, and one control
 *  to undo it — without which a reader who forgot a filter two pages ago reads
 *  a partial table as if it were the whole. */
defineProps({
  /** How many filters are currently set; 0 hides the reset control. */
  active: { type: Number, default: 0 },
})
defineEmits(['reset'])
</script>

<template>
  <div class="filters">
    <slot />
    <span class="spacer" />
    <slot name="end" />
    <button v-if="active" type="button" class="reset" @click="$emit('reset')">
      清除筛选（{{ active }}）
    </button>
  </div>
</template>

<style scoped>
.spacer { flex: 1 1 auto; }
.reset { font-size: var(--fs-aux); }
</style>
