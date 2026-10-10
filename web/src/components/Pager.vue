<script setup>
import { computed } from 'vue'

const props = defineProps({
  total: { type: Number, required: true },
  limit: { type: Number, required: true },
  offset: { type: Number, required: true },
})
const emit = defineEmits(['update:offset'])

const from = computed(() => (props.total ? props.offset + 1 : 0))
const to = computed(() => Math.min(props.offset + props.limit, props.total))
</script>

<template>
  <div class="pager">
    <span class="muted mono">{{ from }}–{{ to }} / {{ total.toLocaleString() }}</span>
    <button :disabled="offset === 0" @click="emit('update:offset', Math.max(0, offset - limit))">上一页</button>
    <button :disabled="to >= total" @click="emit('update:offset', offset + limit)">下一页</button>
  </div>
</template>
