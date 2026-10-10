<script setup>
/**
 * 侧栏顶部的全局检索框。
 *
 * 站里已经有十个页面各带一个搜索框，每个只搜自己那张表。这一个的意义不是再多
 * 一个框，而是**不必先猜这东西属于哪一页**——「募资 295 亿」是事件、是主张还是
 * 公告，那是平台自己做出的分类，读者没有理由预先知道。
 *
 * 浮层的三条做法照抄 `ui/Select.vue`：role=listbox/option、点外部关闭、document
 * 级 Escape。那三条是它踩过坑总结出来的，新写一个下拉不该再踩一遍。
 */
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api.js'
import { debouncedField } from '../debounce.js'
import { isTyping } from '../hotkey.js'
import SearchResults from './SearchResults.vue'

const router = useRouter()
const box = ref(null)
const panel = ref(null)
const input = ref(null)
const open = ref(false)
// 浮层挂到 body 上并用视口坐标定位：侧栏是 sticky 且撑满 100vh，浮层留在里面会被
// 它裁掉一半——实测下拉的右边正好断在侧栏边框上。
const anchor = ref({ top: 0, left: 0 })

async function place() {
  await nextTick()
  const rect = input.value?.getBoundingClientRect()
  if (rect) anchor.value = { top: rect.bottom + 4, left: rect.left }
}

watch(open, (value) => { if (value) place() })
const groups = ref([])
const loading = ref(false)
const failed = ref('')

// 防抖跟列表页共用一份实现：下拉一次要问七张表，其中一路走 FTS，每敲一个字发一次
// 请求在这里比在列表页更糟。
const term = ref({ search: '' })
const draft = debouncedField(term)

watch(() => term.value.search, async (value) => {
  const query = value.trim()
  if (!query) {
    groups.value = []
    open.value = false
    return
  }
  loading.value = true
  failed.value = ''
  try {
    // 下拉给每组三条：够看出「在哪一类里」，不够的走「查看全部」。
    groups.value = (await api.search({ q: query, limit: 3 })).groups
    open.value = true
  } catch (exception) {
    failed.value = exception.message
    groups.value = []
    open.value = true
  } finally {
    loading.value = false
  }
})

function submit() {
  const query = draft.value.trim()
  if (!query) return
  open.value = false
  router.push({ path: '/search', query: { q: query } })
}

function focus() {
  input.value?.focus()
  input.value?.select()
}

function onOutside(event) {
  // 浮层已经挂到 body 上，不再是 box 的后代——只看 box 会让点自己的结果也关掉。
  const inside = box.value?.contains(event.target) || panel.value?.contains(event.target)
  if (!inside) open.value = false
}

function onKey(event) {
  if (event.key === 'Escape') {
    open.value = false
    return
  }
  // `/` 从页面任意处跳到这里。焦点已经在某个控件里时不劫持——那一下是在打字。
  if (event.key === '/' && !isTyping(event.target)) {
    event.preventDefault()
    focus()
  }
}

onMounted(() => {
  document.addEventListener('click', onOutside)
  document.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  document.removeEventListener('click', onOutside)
  document.removeEventListener('keydown', onKey)
})

defineExpose({ focus })
</script>

<template>
  <div ref="box" class="search">
    <input ref="input" v-model="draft" type="search" class="field" role="combobox"
           :aria-expanded="open" aria-controls="search-panel" aria-label="全局检索"
           placeholder="搜索事件、专题、原文"
           title="在任意页面按 / 键，可直接跳到这里检索"
           @keydown.enter.prevent="submit" @focus="open = groups.length > 0" />
    <Teleport to="body">
      <div v-if="open" ref="panel" id="search-panel" class="panel" role="listbox"
           aria-label="检索结果" :style="{ top: `${anchor.top}px`, left: `${anchor.left}px` }">
        <p v-if="failed" class="muted small pad">检索失败：{{ failed }}</p>
        <SearchResults v-else :groups="groups" dense @picked="open = false" />
        <button v-if="!failed" type="button" class="all small" @click="submit">
          查看全部结果 →
        </button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.search { position: relative; margin: 0 0 10px; }
/* min-width 归零：全局给 input[type='search'] 的 220px 是按列表页筛选框定的，
   侧栏内容宽只有 184px，不归零的话框会伸出侧栏右边缘 36px。 */
.field { width: 100%; min-width: 0; box-sizing: border-box; }
.panel {
  position: fixed; z-index: 40;
  width: 320px; max-height: 60vh; overflow-y: auto; padding: 4px 10px;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
}
.pad { padding: 8px 0; }
.all {
  display: block; width: 100%; margin: 4px 0 6px; padding: 5px 0;
  background: none; border: 0; border-top: 1px solid var(--border);
  color: var(--link); cursor: pointer; text-align: left;
}
</style>
