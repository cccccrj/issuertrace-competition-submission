<script setup>
/**
 * 披露差异 — what changed between two versions of the same filing.
 *
 * Migrated onto the shared layer, and two defects fixed while it was open. The
 * company filter was hard-coded to cxmt with no control to change it, so half
 * the corpus was unreachable from this page. And the local label map was
 * missing `not_a_change`, the verdict that says a detected difference is
 * parsing noise rather than a disclosure change — which is exactly the row a
 * reader most needs named.
 */
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { DIFF_KIND_LABELS, api } from '../api.js'
import EChart from '../charts/EChart.vue'
import { partBar } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import Pager from '../components/Pager.vue'
import { debouncedField } from '../debounce.js'

const route = useRoute()
const filters = ref({ company: '', pair: '', kind: '', search: route.query?.search || '' })
const searchDraft = debouncedField(filters)
const offset = ref(0)
const limit = 25
const data = ref(null)
const facets = ref(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [page, counts] = await Promise.all([
      api.diffs({ ...filters.value, limit, offset: offset.value }),
      api.diffFacets({ company: filters.value.company }),
    ])
    data.value = page
    facets.value = counts
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

watch(filters, () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)
load()

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() { filters.value = { company: '', pair: '', kind: '', search: '' } }

const kindOptions = computed(() => (facets.value?.kinds || []).map((row) => ({
  value: row.value, label: DIFF_KIND_LABELS[row.value] || row.value, count: row.count,
})))
const pairOptions = computed(() => (facets.value?.pairs || []).map((row) => ({
  value: row.value, label: row.value, count: row.count,
})))

const kindParts = computed(() => (facets.value?.kinds || []).map((row) => ({
  key: row.value, label: DIFF_KIND_LABELS[row.value] || row.value, count: row.count,
})))

const COLUMNS = [
  { key: 'diff_kind', title: '类型', slot: 'kind', nowrap: true, width: '92px' },
  { key: 'section', title: '章节', width: '150px' },
  { key: 'change', title: '变化', slot: 'change' },
  { key: 'pages', title: '页码', slot: 'pages', nowrap: true, width: '84px' },
]
</script>

<template>
  <PageHeader title="关键信息变化"
      note="同一文件不同版本之间的内容变更。">
  </PageHeader>

  <section v-if="kindParts.length" class="card">
    <h2>差异类型构成</h2>
    <EChart
      :builder="(tokens) => partBar(tokens, { parts: kindParts })"
      label="披露差异按类型的构成"
      :height="54"
      :table-columns="['类型', '差异数']"
      :table-rows="kindParts.map((p) => [p.label, p.count])" />
  </section>

  <FilterBar :active="active" @reset="reset">
    <CompanySelect v-model="filters.company" />
    <EnumSelect v-model="filters.pair" :options="pairOptions" all-label="全部版本对" />
    <EnumSelect v-model="filters.kind" :options="kindOptions" all-label="全部类型" />
    <input v-model="searchDraft" type="search" placeholder="搜索差异内容…" aria-label="搜索差异内容" />
    <template #end>
      <span v-if="data" class="muted small">共 {{ data.total.toLocaleString() }} 条差异</span>
    </template>
  </FilterBar>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    empty-text="没有符合条件的差异。若刚换了公司，该主体可能没有可比的分页 PDF 版本对。"
  >
    <div class="card">
      <DataTable :columns="COLUMNS" :rows="data.items" row-key="diff_id">
        <template #kind="{ row }">
          <span class="tag" :class="row.diff_kind">
            {{ DIFF_KIND_LABELS[row.diff_kind] || row.diff_kind }}
          </span>
        </template>
        <template #change="{ row }">
          <div v-if="row.diff_kind === 'numeric'" class="numeric">
            <span v-if="row.metric" class="metric">{{ row.metric }}</span>
            <span class="mono old">{{ row.old_value }}</span>
            <span class="arrow" aria-hidden="true">→</span>
            <span class="mono new">{{ row.new_value }}</span>
            <span v-if="row.delta_ratio !== null" class="muted mono small">
              （{{ (row.delta_ratio * 100).toFixed(1) }}%）
            </span>
          </div>
          <div v-else>
            <div v-if="row.left_text" class="side removed">{{ row.left_text }}</div>
            <div v-if="row.right_text" class="side added">{{ row.right_text }}</div>
          </div>
          <div class="muted small">{{ row.pair_key }}</div>
        </template>
        <template #pages="{ row }">
          <span class="mono small">
            <span v-if="row.left_page">{{ row.left_page }}</span>
            <span v-if="row.left_page && row.right_page" aria-hidden="true"> → </span>
            <span v-if="row.right_page">{{ row.right_page }}</span>
          </span>
        </template>
      </DataTable>
      <Pager :total="data.total" :limit="limit" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 6px; }
.tag.numeric { color: var(--series-1); border-color: var(--series-1); }
.numeric { display: flex; gap: 7px; align-items: baseline; flex-wrap: wrap; }
.metric { color: var(--text-secondary); }
.old { color: var(--text-muted); text-decoration: line-through; }
.new { color: var(--text-primary); font-weight: 600; }
.arrow { color: var(--text-muted); }
/* A 2px status rail marks the side without relying on a fill colour alone;
   the row's type tag names the change in words. */
.side { border-left: 2px solid var(--gridline); padding: 2px 0 2px 9px; margin-bottom: 3px; font-size: var(--fs-body); }
.side.removed { border-left-color: var(--status-neutral); color: var(--text-muted); }
.side.added { border-left-color: var(--status-good); color: var(--text-primary); }
</style>
