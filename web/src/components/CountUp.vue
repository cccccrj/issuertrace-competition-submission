<script setup>
/** 一个会从 0 滚到终值的数。规则在 `countUp.js`：只滚一次、落在准确终值、reduced-motion 下不动。 */
import { useCountUp } from '../countUp.js'

const props = defineProps({ value: { type: Number, default: null } })
const shown = useCountUp(() => props.value)
</script>

<template>
  <span class="count-up" :aria-label="value === null ? '—' : value.toLocaleString()">{{ shown === null ? '—' : shown.toLocaleString() }}</span>
</template>
