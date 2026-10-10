<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  FROM_KIND_LABELS, api, companyLabel, edgeKindLabel, formatDate, tierLabel
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
import Pager from '../components/Pager.vue'
import PropagationGraph from '../components/PropagationGraph.vue'
import StatTile from '../components/StatTile.vue'
import { debouncedField } from '../debounce.js'

const route = useRoute()
// Arriving from an event detail page means "show me this document's chain", so
// the filter starts from the version in the URL rather than the whole corpus.
const version = ref(route.query.version || '')
const filters = ref({ company: '', search: '' })
const searchDraft = debouncedField(filters)
const minMembers = ref(2)
const offset = ref(0)
const limit = 20
const data = ref(null)
const stats = ref(null)
const loading = ref(true)
const error = ref('')
const expanded = ref(null)
const detail = ref({})

async function load() {
  loading.value = true
  error.value = ''
  try {
    // Asking for one document's chain means showing it even when nothing copied
    // it: "this story spread nowhere" is an answer, not an empty result.
    const [page, summary] = await Promise.all([
      api.components({ ...filters.value, version: version.value,
                       min_members: version.value ? 1 : minMembers.value,
                       limit, offset: offset.value }),
      api.propagationStats({ company: filters.value.company }),
    ])
    data.value = page
    stats.value = summary
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

function clearVersion() {
  version.value = ''
  offset.value = 0
  load()
}

watch(expanded, async (id) => {
  if (!id || detail.value[id]) return
  detail.value = { ...detail.value, [id]: await api.component(id) }
})

watch([filters, minMembers], () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)
load()

const lag = (hours) => {
  if (hours === null || hours === undefined) return '—'
  if (hours < 48) return `${hours.toFixed(1)} 小时`
  return `${(hours / 24).toFixed(1)} 天`
}

const active = computed(() =>
  Object.values(filters.value).filter(Boolean).length + (version.value ? 1 : 0))
function reset() {
  filters.value = { company: '', search: '' }
  version.value = ''
  minMembers.value = 2
}

const MEMBER_OPTIONS = { 2: '至少 2 个版本', 3: '至少 3 个版本', 10: '至少 10 个版本', 1: '含单版本链路' }

/** Edge kinds ranked. The list of counts was a run-on line of text, which is a
 *  poor way to see that one relation type carries most of the graph. */
const edgeKinds = computed(() => (stats.value?.edge_kinds || [])
  .map((kind) => ({ label: edgeKindLabel(kind.value), count: kind.count })))

const COLUMNS = [
  { key: 'root_title', title: '源头版本', slot: 'root' },
  { key: 'member_count', title: '版本数', align: 'right', nowrap: true, width: '78px' },
  { key: 'source_count', title: '来源数', slot: 'sources', align: 'right', nowrap: true, width: '82px' },
  { key: 'span', title: '时间跨度', slot: 'span', nowrap: true, width: '168px' },
]
// The edge list had no header row, so 「0.87」 and 「+4 小时」 sat in unnamed
// columns and a reader had to infer which was which. Naming them costs a row.
const EDGE_COLUMNS = [
  { key: 'edge_kind', title: '关系', slot: 'kind', nowrap: true, width: '96px' },
  { key: 'from_kind', title: '上游', slot: 'from', nowrap: true, width: '84px' },
  { key: 'pair', title: '从 → 到', slot: 'pair' },
  { key: 'similarity', title: '相似度', slot: 'similarity', align: 'right', nowrap: true, width: '76px' },
  { key: 'lag_hours', title: '滞后', slot: 'lag', align: 'right', nowrap: true, width: '80px' },
  { key: 'basis', title: '依据', slot: 'basis' },
]

</script>

<template>
  <PageHeader title="信息传播脉络"
      note="一条消息的转载、改写与回应关系，以及由此得到的独立信源。">
    <template #actions>
      <RouterLink to="/duplicates" class="small">跨源传播聚合 →</RouterLink>
    </template>
  </PageHeader>

  <div v-if="stats" class="tiles">
    <StatTile label="传播链路" :value="stats.multi_member" note="成员多于一条的链路" />
    <StatTile label="跨来源链路" :value="stats.cross_source" note="被其他站点复制过" />
    <StatTile label="最大链路" :value="stats.largest" note="单条链路的版本数" />
    <StatTile label="关系边" :value="stats.edge_kinds.reduce((sum, row) => sum + row.count, 0)" />
  </div>

  <section v-if="edgeKinds.length" class="card">
    <h2>关系类型分布</h2>
    <EChart
      :builder="(tokens) => rankedBars(tokens, { items: edgeKinds })"
      label="传播关系边按类型的分布"
      :height="Math.max(150, edgeKinds.length * 26)"
      :table-columns="['关系类型', '边数']"
      :table-rows="edgeKinds.map((k) => [k.label, k.count])" />
  </section>

  <FilterBar :active="active" @reset="reset">
    <CompanySelect v-model="filters.company" />
    <EnumSelect v-model.number="minMembers" :labels="MEMBER_OPTIONS" all-label="不限版本数" />
    <input v-model="searchDraft" type="search" placeholder="搜索源头标题…" aria-label="搜索源头标题" />
    <button v-if="version" type="button" @click="clearVersion">只看指定文档所在链路 ✕</button>
    <template #end>
      <span v-if="data" class="muted small">共 {{ data.total.toLocaleString() }} 条链路</span>
    </template>
  </FilterBar>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    :empty-text="version
      ? '该文档未被其他版本转载，传播链路仅包含其自身。'
      : '没有符合条件的链路。'"
  >
    <div class="card">
      <DataTable
        :columns="COLUMNS"
        :rows="data.items"
        row-key="component_id"
        expandable
        v-model:expanded="expanded"
      >
        <template #root="{ row }">
          <div>{{ row.root_title || '（无标题）' }}</div>
          <div class="muted small">
            {{ companyLabel(row.company_id) }} · {{ row.root_source_name || '—' }}（{{ tierLabel(row.root_source_tier) }}）
            · {{ row.root_document_type }}
          </div>
        </template>
        <template #sources="{ row }">
          <span class="mono">{{ row.source_count }}</span>
          <span v-if="row.source_count === 1" class="muted small"> 同源</span>
        </template>
        <template #span="{ row }">
          <span class="mono small">
            {{ formatDate(row.first_published_at) }} → {{ formatDate(row.last_published_at) }}
          </span>
        </template>

        <template #expand="{ row }">
          <Loading v-if="!detail[row.component_id]" variant="inline" />
          <template v-else>
            <div class="section-label">传播关系图</div>
            <PropagationGraph
              :members="detail[row.component_id].members"
              :edges="detail[row.component_id].edges" />

            <div class="section-label">链路成员（按传播深度）</div>
            <ol class="members">
              <li v-for="member in detail[row.component_id].members" :key="member.version_id"
                  :style="{ marginLeft: `${member.depth}rem` }">
                <span class="mono small nowrap">{{ formatDate(member.published_at) }}</span>
                <span class="tag nowrap">深度 {{ member.depth }}</span>
                <a v-if="member.canonical_url" :href="member.canonical_url" target="_blank" rel="noopener">
                  {{ member.title || '（无标题）' }}
                </a>
                <span v-else>{{ member.title || '（无标题）' }}</span>
                <span class="muted small nowrap">{{ member.source_name || '—' }}</span>
              </li>
            </ol>

            <div class="section-label">相关关系边（{{ detail[row.component_id].edges.length }}）</div>
            <!-- A plain table rather than a second DataTable: nesting one grid
                 inside another row costs width measurement for no gain. -->
            <DataTable
              :columns="EDGE_COLUMNS" :rows="detail[row.component_id].edges" row-key="edge_id"
            >
              <template #kind="{ row: edge }">
                <span class="tag">{{ edgeKindLabel(edge.edge_kind) }}</span>
              </template>
              <template #from="{ row: edge }">
                <span class="muted small">{{ FROM_KIND_LABELS[edge.from_kind] || edge.from_kind }}</span>
              </template>
              <template #pair="{ row: edge }">
                <span class="small">
                  {{ edge.from_title || '（无标题）' }}
                  <span class="muted">→</span>
                  {{ edge.to_title || '（无标题）' }}
                </span>
              </template>
              <template #similarity="{ row: edge }">
                <span class="mono small">
                  {{ edge.similarity !== null ? edge.similarity.toFixed(2) : '—' }}
                </span>
              </template>
              <template #lag="{ row: edge }"><span class="mono small">{{ lag(edge.lag_hours) }}</span></template>
              <template #basis="{ row: edge }"><span class="muted small">{{ edge.basis }}</span></template>
            </DataTable>
          </template>
        </template>
      </DataTable>
      <Pager :total="data.total" :limit="limit" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 12px; margin-bottom: 12px; }
.section-label { font-size: var(--fs-note); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--text-muted); margin: 4px 0 7px; }
.members { margin: 0 0 14px; padding-left: 18px; font-size: var(--fs-body); }
.members li { padding: 3px 0; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 6px; }
</style>
