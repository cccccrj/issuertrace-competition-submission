<script setup>
/**
 * 议题台账 — the brief's aggregation task at issue level.
 *
 * Migrated onto the shared layer, with two things fixed while it was open. The
 * expanded row printed the verdict counts as badges beside bare numbers while
 * the sibling detail page rendered the same data as a `VerdictBar`; the
 * proportion is now visible in both places, from the component that already
 * gets the ordering and the text legend right. And the page had a single
 * company filter where the data supports status and risk as well — filters the
 * ledger already carries and nobody could reach.
 */
import { computed, ref, watch } from 'vue'

import {
  api, companyLabel, formatDate, issueLabel, levelLabel,
} from '../api.js'
import EChart from '../charts/EChart.vue'
import { rankedBars } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import Loading from '../components/ui/Loading.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import VerdictBar from '../components/VerdictBar.vue'

const filters = ref({ company: '', status: '' })
const data = ref(null)
const loading = ref(true)
const error = ref('')
const expanded = ref(null)
const detail = ref({})

const STATUS_LABELS = {
  emerging: '新出现', escalating: '升温', active: '活跃', cooling: '降温', dormant: '沉寂',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.issues({ ...filters.value })
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

watch(filters, load, { deep: true })
watch(expanded, async (id) => {
  if (!id || detail.value[id]) return
  detail.value = { ...detail.value, [id]: await api.issue(id) }
})
load()

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() { filters.value = { company: '', status: '' } }

/** Independent sources, not article count: the number the aggregation task is
 *  actually about, and the one a reader is most likely to conflate. */
const topSources = computed(() => (data.value?.items || [])
  .slice()
  .sort((a, b) => (b.independent_source_count || 0) - (a.independent_source_count || 0))
  .slice(0, 10)
  .map((issue) => ({ label: issueLabel(issue.issue_code), count: issue.independent_source_count || 0 })))

const COLUMNS = [
  { key: 'issue_code', title: '专题', slot: 'issue', width: '120px' },
  { key: 'company_id', title: '公司', slot: 'company', nowrap: true, width: '90px' },
  { key: 'status', title: '状态', slot: 'status', nowrap: true, width: '80px' },
  { key: 'event_count', title: '事件', align: 'right', nowrap: true, width: '62px' },
  { key: 'document_count', title: '文档', align: 'right', nowrap: true, width: '62px' },
  { key: 'independent_source_count', title: '独立信源', align: 'right', nowrap: true, width: '82px' },
  { key: 'first_seen_at', title: '报道首现', slot: 'first', nowrap: true, width: '110px' },
  { key: 'span', title: '事件发生区间', slot: 'span', nowrap: true, width: '160px' },
  { key: 'risk_score', title: '风险', slot: 'risk', nowrap: true, width: '80px' },
]
</script>

<template>
  <PageHeader title="专题跟踪"
      note="按（公司，专题码）持续追踪的专题，带事件数与独立信源数。">
  </PageHeader>

  <section v-if="topSources.length" class="card">
    <h2>独立信源最多的专题</h2>
    <p class="muted small">
      按<b>独立信源</b>数而非报道篇数排序：同一稿件的多个版本已在近重复聚类中合并，
      报道篇数多不代表多方印证。
    </p>
    <EChart
      :builder="(tokens) => rankedBars(tokens, { items: topSources })"
      label="独立信源数最多的十个专题"
      :height="230"
      :table-columns="['专题', '独立信源', '事件数']"
      :table-rows="(data?.items || []).map((i) => [issueLabel(i.issue_code), i.independent_source_count, i.event_count])" />
  </section>

  <FilterBar :active="active" @reset="reset">
    <CompanySelect v-model="filters.company" />
    <EnumSelect v-model="filters.status" :labels="STATUS_LABELS" all-label="全部状态" />
    <template #end>
      <span v-if="data" class="muted small">共 {{ data.items.length }} 个专题</span>
    </template>
  </FilterBar>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    empty-text="暂无符合条件的专题。专题由事件聚合生成，语料尚未产出事件时此处为空。"
  >
    <div class="card">
      <DataTable
        :columns="COLUMNS"
        :rows="data.items"
        row-key="issue_id"
        expandable
        v-model:expanded="expanded"
      >
        <template #issue="{ row }"><b>{{ issueLabel(row.issue_code) }}</b></template>
        <template #company="{ row }"><span class="secondary">{{ companyLabel(row.company_id) }}</span></template>
        <template #status="{ row }">{{ STATUS_LABELS[row.status] || row.status || '—' }}</template>
        <template #first="{ row }"><span class="mono">{{ formatDate(row.first_seen_at) }}</span></template>
        <template #span="{ row }">
          <span class="mono muted">
            {{ formatDate(row.event_time_earliest) }} → {{ formatDate(row.event_time_latest) }}
          </span>
        </template>
        <template #risk="{ row }">
          <span class="mono">{{ row.risk_score?.toFixed?.(0) ?? '—' }}</span>
          <span class="muted small"> {{ levelLabel(row.risk_level) }}</span>
        </template>

        <template #expand="{ row }">
          <Loading v-if="!detail[row.issue_id]" variant="inline" />
          <div v-else>
            <template v-if="Object.keys(detail[row.issue_id].verdicts || {}).length">
              <b class="block">该专题下的主张裁决</b>
              <!-- The same component the detail page uses: fixed segment order
                   and a counted text legend, so the proportion reads the same
                   way in both places. -->
              <VerdictBar
                :counts="detail[row.issue_id].verdicts"
                :total="Object.values(detail[row.issue_id].verdicts).reduce((a, b) => a + b, 0)" />
            </template>
            <b class="block">时间线（{{ detail[row.issue_id].timeline.length }} 条）</b>
            <ol class="timeline">
              <li v-for="entry in detail[row.issue_id].timeline.slice(0, 30)" :key="entry.event_id">
                <span class="mono nowrap">{{ formatDate(entry.sort_time) }}</span>
                <span class="basis muted nowrap">
                  {{ entry.time_basis === 'event_time' ? '事件时间' : '发布时间' }}
                </span>
                <RouterLink class="entry-summary" :to="`/events/${entry.event_id}`">
                  {{ entry.summary }}
                </RouterLink>
              </li>
            </ol>
            <RouterLink class="small" :to="`/issues/${row.issue_id}`">打开专题详情页 →</RouterLink>
          </div>
        </template>
      </DataTable>
    </div>
  </AsyncSection>
</template>

<style scoped>
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 6px; }
.block { display: block; font-size: var(--fs-aux); margin: 4px 0 6px; }
.timeline { margin: 6px 0 8px; padding-left: 18px; font-size: var(--fs-body); color: var(--text-secondary); }
.timeline li { padding: 3px 0; display: flex; gap: 8px; align-items: baseline; }
.basis { font-size: var(--fs-note); }
.entry-summary { min-width: 0; }
</style>
