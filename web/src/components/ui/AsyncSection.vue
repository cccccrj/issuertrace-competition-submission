<script setup>
/** Loading, failed, empty, or content — the four states every panel has.
 *
 *  `empty` has no default on purpose. This platform's empty states are
 *  statements — 「该事件没有可定位的原子主张，已排除在核验成功率分母之外」 —
 *  not shrugs. A generic 「暂无数据」 would throw away the one place the
 *  interface gets to explain why a table is empty, so every call site is made
 *  to supply its own sentence.
 *
 *  The loading state lives in `Loading.vue` rather than here, because the six
 *  detail drawers need the same words without this component's card. */
import Loading from './Loading.vue'

defineProps({
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  /** True when the request succeeded and returned nothing. */
  empty: { type: Boolean, default: false },
  emptyText: { type: String, required: true },
})
</script>

<template>
  <Loading v-if="loading" />
  <div v-else-if="error" class="card empty" role="alert">{{ error }}</div>
  <div v-else-if="empty" class="card empty"><slot name="empty">{{ emptyText }}</slot></div>
  <slot v-else />
</template>
