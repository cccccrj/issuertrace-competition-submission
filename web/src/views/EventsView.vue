<script setup>
/**
 * 事件台账 — the master ledger.
 *
 * Migrated onto the shared layer. One defect fixed on the way: the filter state
 * carried an `issue` key that no control could set, so an entire dimension the
 * endpoint supports was unreachable — the same shape of bug DiffsView had with
 * its company filter.
 *
 * The risk level stays upper-case here. This endpoint's levels are HIGH/MEDIUM/
 * LOW while the analytical tables use lower case, and api.js says in as many
 * words that the two are read separately on purpose; normalising them would
 * quietly break this filter.
 */
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  ISSUE_LABELS, api, companyLabel, formatDate, targetText, impactText, issueLabel, levelLabel, reviewReasonLabel,
} from '../api.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import Loading from '../components/ui/Loading.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import Pager from '../components/Pager.vue'
import VerdictBadge from '../components/VerdictBadge.vue'
import { debouncedField } from '../debounce.js'
import { fromQuery } from '../deeplink.js'

const route = useRoute()
const BLANK = { company: '', issue: '', risk_level: '', needs_review: '', search: '' }
const filters = ref(fromQuery(BLANK, route.query))
const searchDraft = debouncedField(filters)
const offset = ref(0)
const limit = 25
const data = ref(null)
const loading = ref(true)
const error = ref('')
const expanded = ref(null)
const detail = ref({})

/* 键就是发给接口的值。此前写成大写，而 `event_records.risk_level` 存的是小写、
   列上没有 COLLATE NOCASE，`EnumSelect` 又刻意不做大小写归一（不同端点的大小写
   差异是真实的，归一会弄坏那些要大写的端点）——于是这个下拉选任何一档都返回
   空列表：`WHERE risk_level='HIGH'` 命中 0 行，而库里有 101 行 'high'。 */
const LEVEL_OPTIONS = { high: '高', medium: '中', low: '低' }
const REVIEW_OPTIONS = { true: '需复核', false: '无需复核' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = { ...filters.value, limit, offset: offset.value }
    if (params.needs_review === '') delete params.needs_review
    data.value = await api.events(params)
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

watch(filters, () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)
watch(expanded, async (id) => {
  if (!id || detail.value[id]) return
  detail.value = { ...detail.value, [id]: await api.event(id) }
})
load()

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() {
  filters.value = { ...BLANK }
}

const COLUMNS = [
  { key: 'time', title: '时间', slot: 'time', nowrap: true, width: '108px' },
  { key: 'summary', title: '摘要', slot: 'summary' },
  { key: 'issue_codes', title: '专题', slot: 'issues', width: '130px' },
  { key: 'risk_score', title: '风险', slot: 'risk', nowrap: true, width: '70px' },
  { key: 'needs_review', title: '复核', slot: 'review', nowrap: true, width: '66px' },
  { key: 'go', title: '操作', slot: 'go', nowrap: true, width: '92px' },
]
</script>

<template>
  <!-- 侧栏叫「事件库」，页标题此前叫「事件台账」：同一页两个名字。以侧栏为准。 -->
  <PageHeader title="事件库"
      note="从新闻、公告、监管信息等来源识别并结构化的原始事件。">
  </PageHeader>

  <FilterBar :active="active" @reset="reset">
    <CompanySelect v-model="filters.company" />
    <EnumSelect v-model="filters.issue" :labels="ISSUE_LABELS" all-label="全部专题" />
    <EnumSelect v-model="filters.risk_level" :labels="LEVEL_OPTIONS" all-label="全部风险等级" />
    <EnumSelect v-model="filters.needs_review" :labels="REVIEW_OPTIONS" all-label="全部复核状态" />
    <input v-model="searchDraft" type="search" placeholder="搜索摘要或标题…" aria-label="搜索摘要或标题" />
    <template #end>
      <span v-if="data" class="muted small">共 {{ data.total.toLocaleString() }} 个事件</span>
    </template>
  </FilterBar>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    empty-text="没有符合条件的事件。"
  >
    <div class="card">
      <DataTable
        :columns="COLUMNS"
        :rows="data.items"
        row-key="event_id"
        expandable
        v-model:expanded="expanded"
      >
        <template #time="{ row }">
          <div class="mono">{{ formatDate(row.time_basis === 'event_time' ? row.event_time : row.published_at) }}</div>
          <!-- Naming the clock is not decoration: mixing the two silently would
               misstate the order of events. -->
          <div class="muted basis">{{ row.time_basis === 'event_time' ? '事件时间' : '发布时间' }}</div>
          <!-- 报道时尚在未来的日期是计划或截止，不是发生时间（I-04）。 -->
          <div v-if="row.target_time" class="target">{{ targetText(row.target_time) }}</div>
        </template>
        <template #summary="{ row }">
          <div>{{ row.summary }}</div>
          <div class="muted small"><RouterLink :to="`/companies/${row.company_id}`" class="company">{{ companyLabel(row.company_id) }}</RouterLink> · {{ row.source_name || '—' }}</div>
        </template>
        <template #issues="{ row }">
          <span v-for="code in row.issue_codes.slice(0, 3)" :key="code" class="tag">
            {{ issueLabel(code) }}
          </span>
        </template>
        <template #risk="{ row }">
          <span class="mono">{{ row.risk_score?.toFixed?.(0) ?? '—' }}</span>
          <span class="muted small"> {{ levelLabel(row.risk_level) }}</span>
        </template>
        <template #review="{ row }">
          <span v-if="row.needs_review" class="tag review">需复核</span>
          <span v-else class="muted small">—</span>
        </template>
        <template #go="{ row }">
          <RouterLink :to="`/events/${row.event_id}`">查看事件 →</RouterLink>
        </template>

        <template #expand="{ row }">
          <Loading v-if="!detail[row.event_id]" variant="inline" />
          <div v-else class="detail">
            <dl class="detail-grid">
              <dt>标题</dt>
              <dd>
                <a v-if="detail[row.event_id].canonical_url"
                   :href="detail[row.event_id].canonical_url" target="_blank" rel="noopener">
                  {{ detail[row.event_id].title }}
                </a>
                <span v-else>{{ detail[row.event_id].title }}</span>
              </dd>
              <dt>事件主体</dt>
              <dd>{{ detail[row.event_id].event_subjects.join('、') || '—' }}</dd>
              <dt>影响方向</dt>
              <dd>{{ impactText(detail[row.event_id].impact_direction,
                                 detail[row.event_id].impact_magnitude) }}</dd>
              <dt v-if="detail[row.event_id].amounts.length">金额</dt>
              <dd v-if="detail[row.event_id].amounts.length">
                {{ detail[row.event_id].amounts.join('、') }}
              </dd>
              <dt v-if="detail[row.event_id].review_reasons.length">复核原因</dt>
              <dd v-if="detail[row.event_id].review_reasons.length">
                {{ detail[row.event_id].review_reasons.map(reviewReasonLabel).join('、') }}
              </dd>
            </dl>
            <div v-if="detail[row.event_id].claims.length" class="claims">
              <b>原子主张与裁决</b>
              <div v-for="claim in detail[row.event_id].claims" :key="claim.claim_id" class="claim">
                <VerdictBadge :verdict="claim.verdict" :subtype="claim.partial_subtype" />
                <span class="claim-text">{{ claim.text }}</span>
                <span v-if="claim.evidence_page" class="muted small nowrap">
                  · 第 {{ claim.evidence_page }} 页
                </span>
              </div>
            </div>
            <div v-else class="muted small">
              该事件没有可定位的原子主张，已排除在核验成功率分母之外。
            </div>
            <RouterLink class="small detail-link" :to="`/events/${row.event_id}`">
              打开事件详情页 →
            </RouterLink>
          </div>
        </template>
      </DataTable>
      <Pager :total="data.total" :limit="limit" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.basis { font-size: var(--fs-note); }
.target { display: inline-block; margin-top: 2px; padding: 0 5px; border-radius: 4px; font-size: var(--fs-note);
  color: var(--text-secondary); border: 1px dashed var(--border); white-space: nowrap; }
.tag.review { color: var(--status-serious); border-color: var(--status-serious); }
.claims { margin-top: 12px; font-size: var(--fs-body); }
.claim { display: flex; gap: 8px; align-items: baseline; padding: 5px 0; border-top: 1px solid var(--gridline); }
.claim-text { color: var(--text-secondary); }
.detail-link { display: inline-block; margin-top: 10px; }
</style>
