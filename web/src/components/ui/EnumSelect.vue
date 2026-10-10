<script setup>
/** A select over a controlled vocabulary, from the label map that already
 *  defines it.
 *
 *  Values go over the wire byte-identical to what the API expects. The casing
 *  differences between endpoints are deliberate and documented in api.js — this
 *  component passes the key through untouched rather than normalising it, which
 *  would silently break the endpoints that want upper case.
 *
 *  The empty string is load-bearing: `api.js::request` drops empty params, so
 *  '' is how 「全部」 is expressed on the wire. It stays a real option in the
 *  list rather than a placeholder, because clearing a filter has to be as
 *  reachable as setting one.
 */
import { computed } from 'vue'

import Select from './Select.vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  /** { code: label } — or pass `options` for an explicit list. */
  labels: { type: Object, default: null },
  /** [{ value, label, count? }] — a facets array renders its counts. */
  options: { type: Array, default: null },
  allLabel: { type: String, default: '全部' },
})
defineEmits(['update:modelValue'])

const choices = computed(() => [
  { value: '', label: props.allLabel },
  ...(props.options
    ? props.options.map((option) => ({ ...option, label: String(option.label) }))
    : Object.entries(props.labels || {}).map(([value, label]) => ({ value, label }))),
])
</script>

<template>
  <Select
    :model-value="modelValue" :options="choices" :label="allLabel"
    @update:model-value="$emit('update:modelValue', $event)" />
</template>
