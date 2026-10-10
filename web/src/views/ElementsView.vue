<script setup>
/**
 * 要素抽取与标准化 — the brief's second task, whose second half was invisible.
 *
 * Every event page shows the extracted values; none of them showed whether a
 * value *resolved*. `event_elements` records the raw text, what it normalised
 * to, in what unit, on what basis, and whether it resolved at all — 31,383 rows
 * of it — and the interface read only the flattened arrays that keep the values
 * and drop all of that.
 *
 * The page leads with the resolution rate per type, and it is not flattering
 * everywhere: amounts resolve at 94%, subjects at 41%. Publishing the second
 * number is the point. A standardisation step that reports only its successes
 * is not a measurement.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  BASIS_LABELS, UNIT_LABELS, api, companyLabel, formatDate,
} from '../api.js'
import EChart from '../charts/EChart.vue'
import { rankedBars } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import Pager from '../components/Pager.vue'
import StatTile from '../components/StatTile.vue'
import { debouncedField } from '../debounce.js'
import { fromQuery } from '../deeplink.js'

const stats = ref(null)
const list = ref(null)
const loading = ref(true)
const error = ref('')
const offset = ref(0)
/* `event` 只从别处的链接进来（指南页第二步），界面上没有下拉能设它——所以它自带
   一枚能点掉的按钮。缺了那枚按钮，这一页就会在只显示一条事件的要素时，装作显示的
   是全部三万条。 */
const BLANK = { company: '', element_type: '', status: '', event: '', search: '' }
const route = useRoute()
const filters = ref(fromQuery(BLANK, route.query))
const searchDraft = debouncedField(filters)

const LIMIT = 50

const TYPE_LABELS = {
  subject: '事件主体', time: '时间', amount: '金额', ratio: '比例',
  person: '人物', organization: '机构', product: '产品',
}
const STATUS_LABELS = { normalized: '已标准化', unresolved: '未解析' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [summary, page] = await Promise.all([
      api.elementStats({ company: filters.value.company }),
      api.elements({ ...filters.value, limit: LIMIT, offset: offset.value }),
    ])
    stats.value = summary
    list.value = page
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(filters, () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() { filters.value = { ...BLANK } }
function clearEvent() { filters.value = { ...filters.value, event: '' } }

/** Ordered worst-first: the types that need work are the ones worth looking at,
 *  and a chart sorted by volume would bury them under the ones that are fine. */
const rates = computed(() => (stats.value?.by_type || [])
  .filter((row) => row.normalized_rate !== null)
  .slice()
  .sort((a, b) => a.normalized_rate - b.normalized_rate)
  .map((row) => ({ label: row.label, count: Math.round(row.normalized_rate * 100) })))

const weakest = computed(() => (stats.value?.by_type || [])
  .filter((row) => row.normalized_rate !== null && row.normalized_rate < 0.9)
  .sort((a, b) => a.normalized_rate - b.normalized_rate)[0])

const relative = computed(() =>
  (stats.value?.by_basis || []).find((row) => row.basis === 'relative_to_publication')?.count || 0)

const COLUMNS = [
  { key: 'element_type', title: '类型', slot: 'type', nowrap: true, width: '90px' },
  { key: 'raw_text', title: '原文表述', slot: 'raw' },
  { key: 'normalized_value', title: '标准化结果', slot: 'normalized' },
  { key: 'unit', title: '单位', slot: 'unit', nowrap: true, width: '90px' },
  { key: 'basis', title: '依据', slot: 'basis', nowrap: true, width: '110px' },
  { key: 'event_summary', title: '所属事件', slot: 'event' },
]
</script>

<template>
  <PageHeader title="事件要素抽取与标准化"
      note="从事件原文抽取主体、时间、金额、机构等要素，并统一到标准字段。">
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <div class="tiles">
      <StatTile label="要素总数" :value="stats.totals.elements" />
      <StatTile label="覆盖事件" :value="stats.totals.events" to="/events" />
      <StatTile label="已标准化"
                :value="stats.totals.normalized"
                :note="`整体 ${(stats.totals.normalized_rate * 100).toFixed(1)}%`" />
      <StatTile label="未解析"
                :value="stats.totals.elements - stats.totals.normalized"
                note="保留原文，不猜测" />
    </div>


    <div class="split">
      <section class="card">
        <h2>各类要素的标准化率</h2>
        <EChart
          :builder="(tokens) => rankedBars(tokens, { items: rates })"
          label="各类要素的标准化率，事件主体最低，机构与人物已全部归一"
          :height="220"
          :table-columns="['要素类型', '标准化率（%）', '总数', '未解析']"
          :table-rows="stats.by_type.map((r) => [r.label, r.normalized_rate === null ? '—' : (r.normalized_rate * 100).toFixed(1), r.total, r.unresolved])" />
      </section>

      <section class="card">
        <h2>归一依据</h2>
        <ul class="basis">
          <li v-for="row in stats.by_basis" :key="row.basis">
            <span>{{ BASIS_LABELS[row.basis] || row.basis }}</span>
            <b class="mono">{{ row.count.toLocaleString() }}</b>
          </li>
        </ul>
      </section>
    </div>

    <FilterBar :active="active" @reset="reset">
      <CompanySelect v-model="filters.company" />
      <EnumSelect v-model="filters.element_type" :labels="TYPE_LABELS" all-label="全部类型" />
      <EnumSelect v-model="filters.status" :labels="STATUS_LABELS" all-label="全部状态" />
      <input v-model="searchDraft" type="search" placeholder="搜索原文或标准化结果" aria-label="搜索原文或标准化结果" />
      <button v-if="filters.event" type="button" @click="clearEvent">只看指定事件的要素 ✕</button>
      <template #end>
        <span class="muted small">共 {{ list.total.toLocaleString() }} 条要素</span>
      </template>
    </FilterBar>

    <div class="card">
      <DataTable :columns="COLUMNS" :rows="list.items" row-key="element_id">
        <template #type="{ row }">{{ TYPE_LABELS[row.element_type] || row.element_type }}</template>
        <template #raw="{ row }"><span class="raw">{{ row.raw_text }}</span></template>
        <template #normalized="{ row }">
          <span v-if="row.status === 'normalized'" class="mono">{{ row.normalized_value }}</span>
          <span v-else class="muted">未解析——保留原文</span>
        </template>
        <template #unit="{ row }">
          <span v-if="row.unit">{{ UNIT_LABELS[row.unit] || row.unit }}</span>
          <span v-else class="muted">—</span>
        </template>
        <template #basis="{ row }">
          <span v-if="row.basis" class="tag">{{ BASIS_LABELS[row.basis] || row.basis }}</span>
          <span v-else class="muted">—</span>
        </template>
        <template #event="{ row }">
          <RouterLink :to="`/events/${row.event_id}`">{{ (row.event_summary || '').slice(0, 40) }}</RouterLink>
          <div class="muted small">{{ companyLabel(row.company_id) }} · {{ formatDate(row.event_time) }}</div>
        </template>
      </DataTable>
      <Pager :total="list.total" :limit="LIMIT" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr)); gap: 12px; margin-bottom: 12px; }
.split { display: grid; grid-template-columns: 1.5fr 1fr; gap: 12px; margin-bottom: 12px; }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 8px; }
.raw { color: var(--text-secondary); }
/* 文字在左、数字在右：两列网格，文字列左对齐，数字列右对齐（个位对齐，便于比大小）。
   此前数字在前、在 64px 里右对齐，文字跟在后面，测试反馈读起来是歪的。 */
.basis { list-style: none; margin: 10px 0 0; padding: 0; font-size: var(--fs-body);
  display: grid; grid-template-columns: max-content max-content; column-gap: 24px; row-gap: 4px; }
.basis li { display: contents; }
.basis b { text-align: right; font-variant-numeric: tabular-nums; }
</style>
