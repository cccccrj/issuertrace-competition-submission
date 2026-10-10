<script setup>
/**
 * 采集与入库漏斗 — what was collected, what was kept, and why the rest was not.
 *
 * The event ledger shows what survived. Until now nothing showed what did not:
 * 4,339 document versions were collected and 1,995 events came out, and the
 * difference was invisible. A pipeline that cannot say why it dropped something
 * is one whose output has to be taken on faith.
 *
 * Two things this page is careful about. Rejection codes repeat within a single
 * document, so it reports both how many documents carry a code and how many
 * times it fired — quoting only one of them misstates the same fact. And the
 * per-source event rate is here because a source whose documents never become
 * events is either the wrong source or a parser that has quietly stopped
 * working, and that difference should be noticeable before someone goes looking.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { api, companyLabel, formatDate, tierLabel } from '../api.js'
import EChart from '../charts/EChart.vue'
import { matrixHeatmap, partBar, rankedBars } from '../charts/options/index.js'
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

const funnel = ref(null)
const layers = ref(null)
const sources = ref(null)
const documents = ref(null)
const loading = ref(true)
const error = ref('')
const offset = ref(0)
const route = useRoute()
const filters = ref({ company: '', inclusion_status: '', eventhood: '', layer: '', search: route.query?.search || '' })
const searchDraft = debouncedField(filters)

const LIMIT = 50

const INCLUSION_LABELS = {
  included: '有效入库', excluded_document_type: '文档类型排除',
  excluded_no_body: '无正文', excluded_duplicate: '重复内容',
  excluded_listing_page: '列表页排除',
}
const EVENTHOOD_LABELS = { EVENT: '识别为事件', NO_EVENT: '无事件', IRRELEVANT: '与主体无关' }
const LAYER_LABELS = {
  agree_event: '两层都判为事件', rule_only: '规则层判事件，系统研判否决',
  system_only: '规则层未判，系统研判判为事件', agree_none: '两层都未判为事件',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [f, l, s, d] = await Promise.all([
      api.ingestionFunnel({ company: filters.value.company }),
      api.ingestionLayers({ company: filters.value.company }),
      api.ingestionSources({ company: filters.value.company }),
      api.ingestionDocuments({ ...filters.value, limit: LIMIT, offset: offset.value }),
    ])
    funnel.value = f
    layers.value = l
    sources.value = s
    documents.value = d
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
function reset() {
  filters.value = { company: '', inclusion_status: '', eventhood: '', layer: '', search: '' }
}

/** 规则层与模型的四格对照。
 *
 *  两份读数一直并排存在库里，从来没有被放在一起看过。选 `rule_eventhood`
 *  而不是 `COALESCE(...)`：后者在规则读数缺失时会退回模型自己的答案，
 *  于是矩阵显示完全一致——用没有数据换来的最好看的结论。 */
const layerCase = (key) => (layers.value?.cases || []).find((row) => row.key === key)
const layerCells = computed(() => {
  // [列, 行, 值]：列是模型，行是规则层。
  const at = (key) => layerCase(key)?.count ?? 0
  return [
    [0, 1, at('agree_event')], [1, 1, at('rule_only')],
    [0, 0, at('system_only')], [1, 0, at('agree_none')],
  ]
})
function openLayer(key) {
  filters.value = { ...filters.value, layer: filters.value.layer === key ? '' : key }
}

const stages = computed(() => funnel.value?.stages || [])
const kept = computed(() => {
  const list = stages.value
  return list.length ? Math.round((list.at(-1).count / list[0].count) * 100) : 0
})

const inclusionParts = computed(() => (funnel.value?.inclusion || []).map((row) => ({
  key: row.value, label: INCLUSION_LABELS[row.value] || row.value, count: row.count,
})))

const reasons = computed(() => (funnel.value?.rejection_reasons || [])
  .map((row) => ({ label: row.label, count: row.documents })))

/** Sources that let almost nothing through. Not necessarily broken — an
 *  announcements feed is mostly routine — but the ones worth looking at first. */
const quiet = computed(() => (sources.value?.items || [])
  .filter((row) => row.documents >= 50 && (row.event_rate ?? 1) < 0.25)
  .sort((a, b) => (a.event_rate ?? 0) - (b.event_rate ?? 0)))

const SOURCE_COLUMNS = [
  { key: 'source_name', title: '来源', slot: 'source' },
  { key: 'source_tier', title: '层级', slot: 'tier', nowrap: true, width: '84px' },
  { key: 'documents', title: '文档', align: 'right', nowrap: true, width: '80px' },
  { key: 'included', title: '有效入库', align: 'right', nowrap: true, width: '90px' },
  { key: 'events', title: '成为事件', align: 'right', nowrap: true, width: '90px' },
  { key: 'event_rate', title: '成事率', slot: 'rate', align: 'right', nowrap: true, width: '90px' },
]
const DOC_COLUMNS = [
  { key: 'title', title: '文档', slot: 'title' },
  { key: 'source_name', title: '来源', nowrap: true, width: '150px' },
  { key: 'inclusion_status', title: '入库结果', slot: 'inclusion', nowrap: true, width: '120px' },
  { key: 'eventhood', title: '事件性', slot: 'eventhood', nowrap: true, width: '110px' },
  { key: 'rejection', title: '排除理由', slot: 'reasons' },
]
const REASON_COLUMNS = [
  { key: 'label', title: '理由' },
  { key: 'documents', title: '文档数', slot: 'documents', align: 'right', width: '86px' },
  { key: 'occurrences', title: '出现次数', slot: 'occurrences', align: 'right', width: '92px' },
]

</script>

<template>
  <PageHeader title="多源信息采集与事件转化"
      note="原始信息转化为可分析事件的过程，以及未入库信息的原因说明。">
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <div class="tiles">
      <StatTile v-for="stage in stages" :key="stage.key" :label="stage.label" :value="stage.count" />
      <StatTile label="可核验转化率" :value="`${kept}%`" note="采集文档中最终拆出可核验主张的比例" />
    </div>


    <div class="split">
      <section class="card">
        <h2>入库结果</h2>
        <EChart
          :builder="(tokens) => partBar(tokens, { parts: inclusionParts })"
          label="采集到的文档版本按入库结果的构成"
          :height="54"
          :table-columns="['结果', '文档数']"
          :table-rows="inclusionParts.map((p) => [p.label, p.count])" />
        <h2 class="second">排除理由</h2>
        <DataTable :columns="REASON_COLUMNS" :rows="funnel.rejection_reasons" row-key="code">
          <template #documents="{ row }"><span class="mono">{{ row.documents }}</span></template>
          <template #occurrences="{ row }"><span class="mono">{{ row.occurrences }}</span></template>
        </DataTable>
      </section>

      <section class="card">
        <h2>各来源的产出</h2>
        <EChart
          v-if="sources.items.length"
          :builder="(tokens) => rankedBars(tokens, { items: sources.items.slice(0, 10).map((r) => ({ label: r.source_name || '—', count: r.documents })) })"
          label="文档数最多的十个采集来源"
          :height="240"
          :table-columns="['来源', '文档数', '有效入库', '识别为事件']"
          :table-rows="sources.items.map((r) => [r.source_name || '—', r.documents, r.included || 0, r.events || 0])" />
      </section>
    </div>

    <section v-if="layers?.available" class="card">
      <h2>规则层与系统研判的分歧</h2>
      <p class="muted small rates">
        两层分歧 <b class="mono">{{ (layers.disagreement_rate * 100).toFixed(1) }}%</b>
        （{{ layers.disagreements.toLocaleString() }} / {{ layers.compared.toLocaleString() }} 篇）·
        系统研判否决规则层 <b class="mono">{{ (layers.overrule_rate * 100).toFixed(1) }}%</b>
        （{{ layers.rule_events.toLocaleString() }} 篇中）
      </p>
      <div class="layers">
        <EChart
          :builder="(tokens) => matrixHeatmap(tokens, {
            rows: ['规则层：非事件', '规则层：事件'],
            columns: ['系统研判：事件', '系统研判：非事件'],
            cells: layerCells, valueName: '文档数' })"
          label="规则层与系统研判对事件性判断的四格对照"
          :height="180"
          :table-columns="['对照', '文档数']"
          :table-rows="layers.cases.map((row) => [row.label, row.count])" />
        <ul class="cases">
          <li v-for="row in layers.cases" :key="row.key">
            <button
              type="button" class="case" :class="{ on: filters.layer === row.key, differ: !row.agrees }"
              :aria-pressed="filters.layer === row.key" @click="openLayer(row.key)"
            >
              <b class="mono">{{ row.count.toLocaleString() }}</b>
              <span>{{ row.label }}</span>
            </button>
          </li>
        </ul>
      </div>
    </section>

    <section class="card">
      <h2>来源明细</h2>
      <DataTable :columns="SOURCE_COLUMNS" :rows="sources.items" row-key="source_id">
        <template #tier="{ row }">{{ tierLabel(row.source_tier) }}</template>
        <template #source="{ row }">
          {{ row.source_name || '—' }}
          <div class="muted small">{{ formatDate(row.first_published) }} — {{ formatDate(row.last_published) }}</div>
        </template>
        <template #rate="{ row }">
          <span class="mono">{{ row.event_rate === null ? '—' : `${(row.event_rate * 100).toFixed(1)}%` }}</span>
        </template>
      </DataTable>
    </section>

    <FilterBar :active="active" @reset="reset">
      <CompanySelect v-model="filters.company" />
      <EnumSelect v-model="filters.inclusion_status" :labels="INCLUSION_LABELS" all-label="全部入库结果" />
      <EnumSelect v-model="filters.eventhood" :labels="EVENTHOOD_LABELS" all-label="全部事件性" />
      <EnumSelect v-model="filters.layer" :labels="LAYER_LABELS" all-label="全部分层对照" />
      <input v-model="searchDraft" type="search" placeholder="搜索文档标题" aria-label="搜索文档标题" />
      <template #end>
        <span class="muted small">共 {{ documents.total.toLocaleString() }} 份文档版本</span>
      </template>
    </FilterBar>

    <div class="card">
      <DataTable :columns="DOC_COLUMNS" :rows="documents.items" row-key="version_id">
        <template #title="{ row }">
          <a v-if="row.canonical_url" :href="row.canonical_url" target="_blank" rel="noopener">
            {{ row.title || '（无标题）' }}
          </a>
          <span v-else>{{ row.title || '（无标题）' }}</span>
          <div class="muted small">
            {{ companyLabel(row.company_id) }} · {{ formatDate(row.published_at) }}
            · 正文 {{ row.text_chars }} 字
          </div>
        </template>
        <template #inclusion="{ row }">
          {{ INCLUSION_LABELS[row.inclusion_status] || row.inclusion_status }}
        </template>
        <template #eventhood="{ row }">
          {{ EVENTHOOD_LABELS[row.eventhood] || row.eventhood }}
        </template>
        <template #reasons="{ row }">
          <span v-for="code in row.rejection_reasons" :key="code" class="tag">
            {{ documents.labels.rejection[code] || code }}
          </span>
          <span v-if="!row.rejection_reasons.length" class="muted">—</span>
        </template>
      </DataTable>
      <Pager :total="documents.total" :limit="LIMIT" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 12px; margin-bottom: 12px; }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 8px; }
/* 分歧率与否决率是这一页最有信息量的两个数，此前随整段一起是 11px。抬的只有数，
   括号里的分母留在小字——那是单位不是结果。 */
.rates b { font-size: var(--fs-result); color: var(--text-primary); letter-spacing: -0.01em; }
h2.second { margin-top: 18px; }
.right { text-align: right; }

/* 四格对照：热力图管量级，右边的按钮管「打开这一格」。热力图本身不做点击目标——
   ECharts 的格子在键盘上够不着，而这四格是要被翻开来看的。 */
.layers { display: grid; grid-template-columns: minmax(240px, 1fr) minmax(220px, 320px);
  gap: 16px; align-items: center; }
@media (max-width: 900px) { .layers { grid-template-columns: 1fr; } }
.cases { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.case { display: flex; align-items: baseline; gap: 8px; width: 100%; text-align: left;
  padding: 6px 10px; font: inherit; font-size: var(--fs-aux); color: var(--text-secondary);
  background: none; border: 1px solid var(--gridline); border-radius: 6px; cursor: pointer;
  transition: border-color 0.14s ease, color 0.14s ease; }
.case:hover { border-color: var(--text-muted); color: var(--text-primary); }
.case.on { border-color: var(--text-primary); color: var(--text-primary); }
/* 分歧的两格加一条左边线，不用颜色区分——差异本身由文案说清楚了。 */
.case.differ { border-left-width: 3px; }
.case b { min-width: 3.5em; text-align: right; }
</style>
