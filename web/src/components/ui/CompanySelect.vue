<script setup>
/** The company filter, in one place instead of seven.
 *
 *  Seven views hard-coded `<option value="cxmt">长鑫科技` even though
 *  `COMPANY_LABELS` has lived in api.js the whole time. That duplication is
 *  also a claim about the product: adding a third company meant editing seven
 *  files. Reading the map means it means editing none.
 *
 *  The empty string is load-bearing: `api.js::request` drops empty params, so
 *  '' is how "all companies" is expressed on the wire.
 */
import { computed } from 'vue'

import { COMPANY_LABELS } from '../../api.js'
import Select from './Select.vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  allLabel: { type: String, default: '全部公司' },
})
defineEmits(['update:modelValue'])

const choices = computed(() => [
  { value: '', label: props.allLabel },
  ...Object.entries(COMPANY_LABELS).map(([value, label]) => ({ value, label })),
])
</script>

<template>
  <Select
    :model-value="modelValue" :options="choices" :label="allLabel"
    @update:model-value="$emit('update:modelValue', $event)" />
</template>
