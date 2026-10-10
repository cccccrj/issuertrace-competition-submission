<script setup>
/**
 * 全局检索的结果页。
 *
 * 与顶栏下拉共用 `SearchResults.vue`，区别只有两处：这里每组给 20 条而不是 3
 * 条，且带一个公司筛选——想缩到一家的时候不必回去改词。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api.js'
import { debouncedField } from '../debounce.js'
import SearchResults from '../components/SearchResults.vue'
import AsyncSection from '../components/ui/AsyncSection.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'

const route = useRoute()
const router = useRouter()
const filters = ref({ company: '', search: route.query?.q || '' })
const searchDraft = debouncedField(filters)
const groups = ref([])
const loading = ref(false)
const error = ref('')

async function load() {
  const query = (filters.value.search || '').trim()
  // 地址栏跟着搜索词走：一次检索结果要能被贴给别人。
  if (query !== (route.query?.q || '')) {
    router.replace({ path: '/search', query: query ? { q: query } : {} })
  }
  if (!query) {
    groups.value = []
    return
  }
  loading.value = true
  error.value = ''
  try {
    groups.value = (await api.search({ q: query, limit: 20,
                                       company: filters.value.company || undefined })).groups
  } catch (exception) {
    error.value = exception.message
    groups.value = []
  } finally {
    loading.value = false
  }
}

watch(filters, load, { deep: true, immediate: true })

const found = computed(() => groups.value.reduce((sum, group) => sum + group.items.length, 0))
</script>

<template>
  <PageHeader title="全局检索"
      note="一次问遍事件、核验点、专题、风险事项与原始文档。">
  </PageHeader>

  <FilterBar>
    <input v-model="searchDraft" type="search" class="term" placeholder="检索词…" aria-label="检索词" />
    <CompanySelect v-model="filters.company" />
  </FilterBar>

  <section class="card">
    <AsyncSection :loading="loading" :error="error" :empty="!found && !!filters.search"
                  empty-text="没有匹配的结果。换个说法，或去对应页面用它自己的筛选条件找。">
      <SearchResults :groups="groups" />
    </AsyncSection>
    <p v-if="!filters.search" class="muted small">输入检索词，或在任意页面按 <kbd>/</kbd> 唤起检索框。</p>
  </section>
</template>

<style scoped>
.term { min-width: 260px; }
kbd { padding: 0 4px; border: 1px solid var(--border); border-radius: 3px; font-size: var(--fs-note); }
</style>
