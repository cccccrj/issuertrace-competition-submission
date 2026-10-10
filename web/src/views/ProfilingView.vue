<script setup>
/**
 * 稿件画像与辨伪 — the reasoning behind two labels the interface reduced to one
 * word each.
 *
 * Both sidecars record, per event, the label, the model's stated reason, its
 * confidence, the basis it rests on, and the model and prompt version that
 * produced it. The pages showed the word and dropped the rest, which is the
 * same defect those sidecars were written to repair: a value with no stated
 * basis is a value a reader has to take on faith.
 *
 * The cross-tab is the point of the page rather than a decoration. Tone and
 * impact are output separately and allowed to disagree — a dryly-worded penalty
 * filing is neutral in tone and negative in impact — and before the repair they
 * were the same value on all 2,045 rows, which meant one of them was not being
 * judged at all. 29.9% disagreement is the evidence that it now is.
 */
import { computed, onMounted, ref, watch } from 'vue'

import {
  ARTICLE_SENTIMENT_LABELS, LEVEL_META, api, companyLabel, formatDate,
} from '../api.js'
import EChart from '../charts/EChart.vue'
import { matrixHeatmap, rankedBars, sizeHistogram } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import TechDetails from '../components/TechDetails.vue'
import Pager from '../components/Pager.vue'
import StatTile from '../components/StatTile.vue'
import { debouncedField } from '../debounce.js'

const tab = ref('sentiment')
const stats = ref(null)
const list = ref(null)
const loading = ref(true)
const error = ref('')
const offset = ref(0)
const filters = ref({ company: '', value: '', basis: '', search: '' })
const searchDraft = debouncedField(filters)

const LIMIT = 25

/* 本地那份写的是「正面语气」，共享表写的是「正面」——同一个码两种说法，
   正是 tests/test_labels.py 存在的理由。这里带上「语气」二字作为语境后缀。 */
const SENTIMENT_LABELS = Object.fromEntries(
  Object.entries(ARTICLE_SENTIMENT_LABELS).map(([code, label]) => [code, `${label}语气`]))
const IMPACT_LABELS = { positive: '正面影响', neutral: '中性影响', negative: '负面影响', uncertain: '方向不明' }
const BASIS_LABELS = { system: '系统研判', propagation_edge: '由传播关系推定' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const statsCall = tab.value === 'sentiment' ? api.sentimentStats : api.productionStats
    const listCall = tab.value === 'sentiment' ? api.sentimentLabels : api.productionLabels
    const [summary, page] = await Promise.all([
      statsCall({ company: filters.value.company }),
      listCall({ ...filters.value, limit: LIMIT, offset: offset.value }),
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
watch(tab, () => { offset.value = 0; filters.value.value = ''; load() })
watch(filters, () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() { filters.value = { company: '', value: '', basis: '', search: '' } }

const valueLabels = computed(() => list.value?.labels?.value || {})

const values = computed(() => (stats.value?.by_value || [])
  .map((row) => ({ label: row.label, count: row.count })))

/** Confidence as a spread rather than a mean: a model answering 0.95 to
 *  everything and one that varies are different instruments. */
const confidence = computed(() => (stats.value?.confidence || [])
  .map((row) => ({ label: row.bucket?.toFixed(2), count: row.count })))

const TONES = ['positive', 'neutral', 'negative']
const IMPACTS = ['positive', 'neutral', 'negative', 'uncertain']

const crossTab = computed(() => {
  const rows = stats.value?.vs_impact || []
  const cells = []
  TONES.forEach((tone, y) => {
    IMPACTS.forEach((impact, x) => {
      const found = rows.find((row) => row.tone === tone && row.impact === impact)
      cells.push([x, y, found?.count || 0])
    })
  })
  return cells
})

const sponsored = computed(() => (stats.value?.vs_sentiment || [])
  .filter((row) => row.mode === 'SPONSORED'))

</script>

<template>
  <PageHeader title="报道属性与可信分析"
      note="生产方式与情感倾向两个标注，连同它们各自的依据。">
    <template #actions>
      <button type="button" :class="{ on: tab === 'sentiment' }" @click="tab = 'sentiment'">语言倾向</button>
      <button type="button" :class="{ on: tab === 'production' }" @click="tab = 'production'">生产方式</button>
    </template>
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <div class="tiles">
      <StatTile label="已标注事件" :value="stats.totals.labelled" />
      <StatTile
        v-if="tab === 'sentiment' && stats.divergence"
        label="语气与影响分歧"
        :value="stats.divergence.diverge"
        :note="`${(stats.divergence.rate * 100).toFixed(1)}%——修复前为 0%`" />
      <StatTile
        v-for="row in stats.by_value.slice(0, 3)"
        :key="row.value"
        :label="row.label"
        :value="row.count" />
    </div>



    <div class="split">
      <section class="card">
        <h2>{{ tab === 'sentiment' ? '语气分布' : '生产方式分布' }}</h2>
        <EChart
          :builder="(tokens) => rankedBars(tokens, { items: values })"
          :label="tab === 'sentiment' ? '事件按稿件语气的分布' : '事件按稿件生产方式的分布'"
          :height="200"
          :table-columns="['取值', '事件数']"
          :table-rows="values.map((v) => [v.label, v.count])" />
      </section>

      <section v-if="tab === 'sentiment'" class="card">
        <h2>语气 × 影响方向</h2>
        <EChart
          :builder="(tokens) => matrixHeatmap(tokens, { rows: TONES.map((t) => SENTIMENT_LABELS[t]), columns: IMPACTS.map((i) => IMPACT_LABELS[i]), cells: crossTab, valueName: '事件数' })"
          label="稿件语气与事件影响方向的交叉分布，中性语气配负面影响是方案举的样板格"
          :height="200"
          :table-columns="['语气', '影响方向', '事件数']"
          :table-rows="(stats.vs_impact || []).map((r) => [SENTIMENT_LABELS[r.tone] || r.tone, IMPACT_LABELS[r.impact] || r.impact, r.count])" />
      </section>

      <section v-else class="card">
        <h2>判定依据</h2>
        <ul class="basis">
          <li v-for="row in stats.by_basis" :key="row.value">
            <b class="mono">{{ row.count }}</b><span>{{ row.label }}</span>
          </li>
        </ul>
        <h2 class="second">置信度分布</h2>
        <EChart
          v-if="confidence.length"
          :builder="(tokens) => sizeHistogram(tokens, { buckets: confidence, bucketName: '置信度', valueName: '事件数' })"
          label="系统自评置信度的分布"
          :height="150"
          :table-columns="['置信度', '事件数']"
          :table-rows="confidence.map((c) => [c.label, c.count])" />
      </section>
    </div>

    <FilterBar :active="active" @reset="reset">
      <CompanySelect v-model="filters.company" />
      <EnumSelect v-model="filters.value" :labels="valueLabels" all-label="全部取值" />
      <EnumSelect v-model="filters.basis" :labels="BASIS_LABELS" all-label="全部依据" />
      <input v-model="searchDraft" type="search" placeholder="搜索判据或事件摘要" aria-label="搜索判据或事件摘要" />
      <template #end>
        <span class="muted small">共 {{ list.total.toLocaleString() }} 条</span>
      </template>
    </FilterBar>

    <div class="card">
      <DataTable
        :columns="[
          { key: 'value', title: '判定', slot: 'value', nowrap: true, width: '120px' },
          { key: 'confidence', title: '置信度', slot: 'confidence', align: 'right', nowrap: true, width: '84px' },
          { key: 'reason', title: '判据（系统给出的理由）', slot: 'reason' },
          { key: 'summary', title: '事件', slot: 'event' },
        ]"
        :rows="list.items"
        row-key="event_id"
      >
        <template #value="{ row }">
          <div>{{ valueLabels[row.value] || row.value }}</div>
          <div v-if="tab === 'sentiment'" class="muted small">
            影响：{{ IMPACT_LABELS[row.impact_direction] || row.impact_direction }}
          </div>
          <div v-else class="muted small">{{ BASIS_LABELS[row.basis] || row.basis }}</div>
        </template>
        <template #confidence="{ row }">
          <span class="mono">{{ row.confidence?.toFixed?.(2) ?? '—' }}</span>
        </template>
        <template #reason="{ row }">
          <span class="reason">{{ row.reason || '（未记录判据）' }}</span>
        </template>
        <template #event="{ row }">
          <RouterLink :to="`/events/${row.event_id}`">{{ (row.summary || '').slice(0, 36) }}</RouterLink>
          <div class="muted small">
            {{ companyLabel(row.company_id) }} · {{ row.source_name || '—' }} · {{ formatDate(row.event_time) }}
          </div>
        </template>
      </DataTable>
      <Pager :total="list.total" :limit="LIMIT" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr)); gap: 12px; margin-bottom: 12px; }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 8px; }
h2.second { margin-top: 18px; }
.right { text-align: right; }
.reason { font-size: var(--fs-body); color: var(--text-secondary); }
.basis { list-style: none; margin: 0; padding: 0; font-size: var(--fs-body); }
.basis li { display: flex; gap: 8px; margin-bottom: 4px; }
.basis b { min-width: 52px; text-align: right; }
.provenance { margin-bottom: 12px; }
button.on { border-color: var(--series-1); color: var(--text-primary); }
</style>
