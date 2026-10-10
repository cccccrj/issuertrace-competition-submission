<script setup>
/**
 * 同源聚合与去重 — the brief's fourth task, and the one the interface served
 * most thinly. The clustering has been running since the beginning; its output
 * was visible only as a member count on a propagation row.
 *
 * The page has one job beyond showing the numbers: keeping two ideas apart that
 * look identical from a distance. A **near-duplicate cluster** groups by
 * similarity — the same text arriving through several doors. A **propagation
 * component** groups by copy relations — who republished whom. Ten outlets
 * quoting one prospectus are ten independent stories in one cluster; ten
 * reposts are one story. Conflating them is how an independent-source count
 * gets inflated, so both are shown on every cluster and labelled.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  CLUSTER_KIND_LABELS, api, companyLabel, formatDate, tierLabel
} from '../api.js'
import EChart from '../charts/EChart.vue'
import { sizeHistogram } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import Loading from '../components/ui/Loading.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import TechDetails from '../components/TechDetails.vue'
import Pager from '../components/Pager.vue'
import StatTile from '../components/StatTile.vue'
import { debouncedField } from '../debounce.js'
import { fromQuery, wanted } from '../deeplink.js'
import { SOURCE_TIER_GLOSS } from '../glossary.js'

const stats = ref(null)
const list = ref(null)
const detail = ref({})
const loading = ref(true)
const error = ref('')
const expanded = ref(null)
const offset = ref(0)
/* 点名一个簇是从别处链接进来的（指南页第四步），界面上没有控件能设它，所以它带
   一枚能点掉的按钮。接口在收到 cluster 时会绕开 member_count 水位——那条 URL 的
   意思是「让我看这一个」，不该因为水位没跟着调对而落到空列表上。 */
const BLANK = { company: '', cluster: '', search: '' }
const route = useRoute()
const filters = ref(fromQuery(BLANK, route.query))
const searchDraft = debouncedField(filters)

const LIMIT = 25

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [summary, page] = await Promise.all([
      api.duplicateStats({ company: filters.value.company }),
      api.duplicates({ ...filters.value, limit: LIMIT, offset: offset.value }),
    ])
    stats.value = summary
    list.value = page
    /* 被点名的那一簇直接展开。先确认它真的在这一页上：把一个不在列表里的 id 塞进
       `expanded`，读者看到的不是错误，是一个永远转不完的加载态。 */
    const open = wanted(filters.value, 'cluster', page.items, 'cluster_id')
    if (open && expanded.value !== open) expanded.value = open
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(filters, () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)
watch(expanded, async (id) => {
  if (!id || detail.value[id]) return
  detail.value = { ...detail.value, [id]: await api.duplicate(id) }
})

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() { filters.value = { ...BLANK } }
function clearCluster() { filters.value = { ...filters.value, cluster: '' } }

/** The tail is long — 188 clusters of two against one of 209 — so drawing forty
 *  columns of height one would hide the shape rather than show it. */
const buckets = computed(() => {
  const raw = stats.value?.size_histogram || []
  const bands = [['2', (n) => n === 2], ['3–4', (n) => n >= 3 && n <= 4],
                 ['5–9', (n) => n >= 5 && n <= 9], ['10–29', (n) => n >= 10 && n <= 29],
                 ['30–99', (n) => n >= 30 && n <= 99], ['100+', (n) => n >= 100]]
  return bands.map(([label, test]) => ({
    label,
    count: raw.filter((row) => test(row.members)).reduce((sum, row) => sum + row.clusters, 0),
  }))
})

const COLUMNS = [
  { key: 'member_count', title: '版本数', slot: 'members', align: 'right', nowrap: true, width: '110px' },
  { key: 'representative_title', title: '代表版本', slot: 'title' },
  { key: 'company_id', title: '公司', slot: 'company', nowrap: true, width: '110px' },
  { key: 'similarity_threshold', title: '相似度门限', align: 'right', nowrap: true, width: '110px' },
  { key: 'published_at', title: '发布时间', slot: 'time', nowrap: true, width: '130px' },
]
const MEMBER_COLUMNS = [
  { key: 'published_at', title: '发布时间', slot: 'published', nowrap: true, width: '140px' },
  { key: 'title', title: '标题', slot: 'title' },
  { key: 'source_id', title: '来源', nowrap: true, width: '120px' },
  { key: 'source_tier', title: '层级', slot: 'tier', nowrap: true, width: '70px' },
]

</script>

<template>
  <PageHeader title="跨源传播聚合"
      note="近重复内容归并后的簇，同一稿件的多处转载仅计一次。">
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <div class="tiles">
      <StatTile label="同源簇" :value="stats.clusters" />
      <StatTile label="归入簇的版本" :value="stats.members" />
      <StatTile label="被折叠掉的版本" :value="stats.collapsed"
                note="归并后不再单独计为一条信源" />
      <StatTile label="最大簇" :value="stats.largest?.member_count || 0" note="单簇内的版本数" />
      <StatTile label="文档版本总数" :value="stats.documents" to="/events" />
    </div>

    <div class="split">
      <section class="card">
        <h2>簇规模分布</h2>
        <EChart
          :builder="(tokens) => sizeHistogram(tokens, { buckets, bucketName: '簇内版本数', valueName: '簇数' })"
          label="同源簇按簇内版本数的分布，两篇一簇占绝大多数，长尾延伸到一百篇以上"
          :height="200"
          :table-columns="['簇内版本数', '簇数']"
          :table-rows="buckets.map((b) => [b.label, b.count])" />
      </section>

      <section class="card">
        <h2>口径</h2>
        <!-- 聚类方式与门限是算法参数，收进折叠；「按公司」是数据，留在外面。
             逐簇的门限仍然是主表的一列——那是每个簇各自的取值，不是全局参数。 -->
        <TechDetails :items="[
          { label: '聚类方式', value: CLUSTER_KIND_LABELS[stats.threshold?.kind] || stats.threshold?.kind },
          { label: '相似度门限', value: stats.threshold ? `${stats.threshold.low} – ${stats.threshold.high}` : '' },
        ]" />
        <dl class="detail-grid">
          <dt>按公司</dt>
          <dd>
            <div v-for="row in stats.by_company" :key="row.company_id">
              {{ companyLabel(row.company_id) }}：{{ row.clusters }} 簇 / {{ row.members }} 个版本
            </div>
          </dd>
        </dl>
      </section>
    </div>

    <FilterBar :active="active" @reset="reset">
      <CompanySelect v-model="filters.company" />
      <input v-model="searchDraft" type="search" placeholder="搜索代表版本标题" aria-label="搜索代表版本标题" />
      <button v-if="filters.cluster" type="button" @click="clearCluster">只看指定同源簇 ✕</button>
      <template #end>
        <span class="muted small">共 {{ list.total }} 簇</span>
      </template>
    </FilterBar>

    <div class="card">
      <DataTable
        :columns="COLUMNS"
        :rows="list.items"
        row-key="cluster_id"
        expandable
        v-model:expanded="expanded"
      >
        <!-- 这一列此前只是个右对齐的数字。「只能看到有多少个，不能看到具体哪些」
             —— 成员表一直在展开行里，缺的是屏幕上说出「这个数点得开」。 -->
        <template #members="{ row }">
          <span class="mono">{{ row.member_count }}</span>
          <span class="open-hint"> 个版本 ›</span>
        </template>
        <template #title="{ row }">
          <a v-if="row.canonical_url" :href="row.canonical_url" target="_blank" rel="noopener">
            {{ row.representative_title || '（无标题）' }}
          </a>
          <span v-else>{{ row.representative_title || '（无标题）' }}</span>
          <div class="muted small">{{ row.representative_source_name || '—' }} · {{ tierLabel(row.representative_tier) }}</div>
        </template>
        <template #company="{ row }">{{ companyLabel(row.company_id) }}</template>
        <template #time="{ row }">{{ formatDate(row.published_at) }}</template>

        <template #expand="{ row }">
          <Loading v-if="!detail[row.cluster_id]" variant="inline" />
          <div v-else class="detail">
            <Callout tone="info">
              该簇含 <b class="mono">{{ detail[row.cluster_id].members.length }}</b> 个版本，
              来自 <b class="mono">{{ detail[row.cluster_id].distinct_sources }}</b> 家发布方；
              其中涉及 <b class="mono">{{ detail[row.cluster_id].components.length }}</b> 条传播链路。
              <template v-if="detail[row.cluster_id].distinct_sources === 1">
                单一来源——同一家反复发布，不构成多源印证。
              </template>
            </Callout>
            <DataTable
              :columns="MEMBER_COLUMNS"
              :rows="detail[row.cluster_id].members.slice(0, 40)"
              row-key="version_id"
            >
              <template #published="{ row: member }">{{ formatDate(member.published_at) }}</template>
              <template #title="{ row: member }">
                <a v-if="member.canonical_url" :href="member.canonical_url" target="_blank" rel="noopener">
                  {{ member.title || '（无标题）' }}
                </a>
                <span v-else>{{ member.title || '（无标题）' }}</span>
              </template>
              <template #tier="{ row: member }"><span class="mono glossed"
                :title="SOURCE_TIER_GLOSS[member.source_tier] || ''">{{ tierLabel(member.source_tier) }}</span></template>
            </DataTable>
            <p v-if="detail[row.cluster_id].members.length > 40" class="muted small">
              仅列出前 40 个版本，共 {{ detail[row.cluster_id].members.length }} 个。
              如需查看全部，
              <a :href="`/api/duplicates/${row.cluster_id}`" target="_blank" rel="noopener">获取该簇的完整数据</a>。
            </p>
          </div>
        </template>
      </DataTable>
      <Pager :total="list.total" :limit="LIMIT" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 12px; }
.split { display: grid; grid-template-columns: 1.4fr 1fr; gap: 12px; margin: 12px 0; }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 8px; }
/* 跟着行的悬停/展开态一起变深，与 DataTable 的箭头同步。 */
.open-hint { color: var(--text-muted); font-size: var(--fs-note); }
:deep(tr.clickable:hover) .open-hint, :deep(tr.open) .open-hint { color: var(--series-1); }
.detail { padding: 4px 0; }
</style>
