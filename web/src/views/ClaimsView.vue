<script setup>
/**
 * 主张核验 — the credibility task, and the page the demonstration walks through.
 *
 * Migrated onto the shared layer, with four things fixed while it was open:
 *
 * * The company filter defaulted to cxmt, so the page opened on half the corpus
 *   without saying so.
 * * The notice text contained literal Markdown — `**尚未采集权威披露语料**`
 *   rendered with its asterisks showing.
 * * The issue filter was built from whatever happened to be on the loaded page,
 *   so its options changed as you paged. It reads the vocabulary now.
 * * `partial_subtype` was filterable on the API from this round's work and had
 *   no control here, which is the one thing 交付物14 §3.2 asks the presenter to
 *   do — 「筛 partial_subtype = already_explained」.
 *
 * The method mix beside the notice is another thing that was being fetched and
 * discarded: /api/claims/stats returns it, and only `no_corpus` was read.
 */
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  DIFF_KIND_LABELS, ISSUE_LABELS, METHOD_LABELS, PARTIAL_SUBTYPE_LABELS, UNVERIFIABLE_SUBTYPE_LABELS,
  VERDICT_META, api, companyLabel, formatDate, issueLabel,
} from '../api.js'
import EChart from '../charts/EChart.vue'
import { partBar } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import Loading from '../components/ui/Loading.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import EvidenceCite from '../components/EvidenceCite.vue'
import Pager from '../components/Pager.vue'
import InquiryChain from '../components/InquiryChain.vue'
import NumericCheck from '../components/NumericCheck.vue'
import VerdictBadge from '../components/VerdictBadge.vue'
import { debouncedField } from '../debounce.js'
import { fromQuery } from '../deeplink.js'

const route = useRoute()
/* `event` 没有对应的下拉：它只从别处的链接进来（指南页的第三步、事件详情页）。
   即便如此也留在 filters 里，而不是像传播链路页的 `version` 那样单开一个 ref——
   它就是 /api/claims 的一个普通参数，放进来「清除筛选」一次赋值就够，另开一个 ref
   会让 reset 同时惊动两个 watcher，发两遍同样的请求。
   代价是它必须自己带一枚能点掉的按钮：一个悄悄只显示语料一小部分的页面，得在屏幕
   上说出这件事。 */
const BLANK = { company: '', verdict: '', issue: '', method: '', partial_subtype: '',
                unverifiable_subtype: '', unverifiable_group: '', event: '', search: '' }
const filters = ref(fromQuery(BLANK, route.query))
const searchDraft = debouncedField(filters)
const offset = ref(0)
const limit = 25
const data = ref(null)
const stats = ref(null)
const loading = ref(true)
const error = ref('')
const expanded = ref(null)
const detail = ref({})

const VERDICT_OPTIONS = Object.fromEntries(
  Object.entries(VERDICT_META).map(([key, meta]) => [key, meta.label]))
/** 取 api.js 那一份。此前这里自带一份，七个子类里五个的说法与 api.js 不同（「时点不符」
 *  对「时间错位」），同一个子类在这一页和评测页上是两个名字。`already_explained` 是演示
 *  要用的那一刀，下拉里多带一句解释。 */
const SUBTYPE_LABELS = {
  ...PARTIAL_SUBTYPE_LABELS,
  already_explained: `${PARTIAL_SUBTYPE_LABELS.already_explained}（交易所已问、公司已答）`,
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [page, summary] = await Promise.all([
      api.claims({ ...filters.value, limit, offset: offset.value }),
      api.claimStats({ company: filters.value.company }),
    ])
    data.value = page
    stats.value = summary
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
  detail.value = { ...detail.value, [id]: await api.claim(id) }
})
load()

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() {
  filters.value = { ...BLANK }
}
function clearEvent() {
  filters.value = { ...filters.value, event: '' }
}

/** Fetched all along and never rendered: which adjudicator settled each claim.
 *  A rule-decided verdict and a model-decided one are different kinds of fact. */
const methodParts = computed(() => Object.entries(stats.value?.methods || {})
  .map(([key, count]) => ({ key, label: METHOD_LABELS[key] || key, count })))

/** 无法核验的构成：设计边界几条、可改进几条，每一类点下去就筛到那一类。
 *  服务端还没拆分（库没跑过 processor.unverifiable）时 `available` 为 false，这一块不出现。 */
const unverifiable = computed(() => stats.value?.unverifiable?.available ? stats.value.unverifiable : null)
const unverifiableParts = computed(() => (unverifiable.value?.subtypes || [])
  .filter((row) => row.count)
  .map((row) => ({ key: row.code, label: row.label, count: row.count })))
function pickUnverifiable(patch) {
  filters.value = { ...filters.value, verdict: 'UNVERIFIABLE', unverifiable_subtype: '',
                    unverifiable_group: '', ...patch }
}

const COLUMNS = [
  { key: 'verdict', title: '裁决', slot: 'verdict', nowrap: true, width: '104px' },
  { key: 'text', title: '主张', slot: 'text' },
  { key: 'issue_code', title: '专题', slot: 'issue', nowrap: true, width: '84px' },
  { key: 'evidence', title: '证据', slot: 'evidence', width: '170px' },
  { key: 'method', title: '方式', slot: 'method', nowrap: true, width: '96px' },
]
</script>

<template>
  <PageHeader title="证据核验明细"
      note="事件拆出的原子主张，以及每条与披露原文比对后的裁决。">
  </PageHeader>

  <Callout v-if="stats?.no_corpus" tone="warn">
    该主体有 {{ stats.no_corpus.toLocaleString() }} 条主张因<b>尚未采集权威披露语料</b>而无法核验。
  </Callout>

  <section v-if="methodParts.length" class="card">
    <h2>裁决方式构成</h2>
    <EChart
      :builder="(tokens) => partBar(tokens, { parts: methodParts })"
      label="主张裁决按裁决方式的构成"
      :height="54"
      :table-columns="['方式', '主张数']"
      :table-rows="methodParts.map((p) => [p.label, p.count])" />
  </section>

  <section v-if="unverifiable" class="card unverifiable">
    <h2>「无法核验」的构成</h2>
    <p class="muted small">
      「无法核验」不等同于失败率：其中一部分属于设计边界之外、本不应核验的内容，另一部分可通过补充资料或改进检索加以改善。
    </p>
    <div class="groups">
      <button v-for="group in unverifiable.groups" :key="group.code" type="button" class="group"
              :class="[group.code, { on: filters.unverifiable_group === group.code }]"
              @click="pickUnverifiable({ unverifiable_group: group.code })">
        <b class="count">{{ group.count.toLocaleString() }}</b>
        <span>{{ group.label }}</span>
      </button>
    </div>
    <EChart
      v-if="unverifiableParts.length"
      :builder="(tokens) => partBar(tokens, { parts: unverifiableParts })"
      label="无法核验的主张按子类的构成"
      :height="54"
      :table-columns="['子类', '主张数']"
      :table-rows="unverifiableParts.map((p) => [p.label, p.count])" />
    <ul class="subtypes">
      <li v-for="row in unverifiable.subtypes.filter((item) => item.count)" :key="row.code">
        <button type="button" class="link" @click="pickUnverifiable({ unverifiable_subtype: row.code })">
          {{ row.label }}
        </button>
        <span class="mono">{{ row.count.toLocaleString() }}</span>
        <span class="muted small">{{ row.group === 'boundary' ? '设计边界' : '可改进' }}</span>
      </li>
    </ul>
  </section>

  <FilterBar :active="active" @reset="reset">
    <CompanySelect v-model="filters.company" />
    <EnumSelect v-model="filters.verdict" :labels="VERDICT_OPTIONS" all-label="全部裁决" />
    <EnumSelect v-model="filters.partial_subtype" :labels="SUBTYPE_LABELS" all-label="全部部分支持子类" />
    <EnumSelect v-model="filters.unverifiable_subtype" :labels="UNVERIFIABLE_SUBTYPE_LABELS"
                all-label="全部无法核验子类" />
    <EnumSelect v-model="filters.method" :labels="METHOD_LABELS" all-label="全部方式" />
    <EnumSelect v-model="filters.issue" :labels="ISSUE_LABELS" all-label="全部专题" />
    <input v-model="searchDraft" type="search" placeholder="搜索主张内容…" aria-label="搜索主张内容" />
    <button v-if="filters.event" type="button" @click="clearEvent">只看指定事件的主张 ✕</button>
    <template #end>
      <span v-if="data" class="muted small">共 {{ data.total.toLocaleString() }} 条主张</span>
    </template>
  </FilterBar>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    empty-text="没有符合条件的主张。"
  >
    <div class="card">
      <DataTable
        :columns="COLUMNS"
        :rows="data.items"
        row-key="claim_id"
        expandable
        v-model:expanded="expanded"
      >
        <template #verdict="{ row }">
          <VerdictBadge :verdict="row.verdict" :subtype="row.partial_subtype" />
          <div v-if="row.unverifiable_label" class="why-not small" :class="row.unverifiable_group"
               :title="row.unverifiable_basis">{{ row.unverifiable_label }}</div>
        </template>
        <template #text="{ row }">
          <div>{{ row.text }}</div>
          <div class="muted small">
            {{ companyLabel(row.company_id) }} · {{ formatDate(row.event_time) }}
          </div>
        </template>
        <template #issue="{ row }"><span class="tag">{{ issueLabel(row.issue_code) }}</span></template>
        <template #evidence="{ row }">
          <template v-if="row.evidence">
            <div class="secondary small">
              <a v-if="row.evidence.url" :href="`${row.evidence.url}#page=${row.evidence.page}`"
                 target="_blank" rel="noopener">《{{ row.evidence.title }}》</a>
              <span v-else>《{{ row.evidence.title }}》</span>
            </div>
            <div class="mono small">第 {{ row.evidence.page }} 页</div>
          </template>
          <span v-else class="muted small">—</span>
        </template>
        <template #method="{ row }">
          <span class="muted small">{{ METHOD_LABELS[row.method] || row.method || '—' }}</span>
        </template>

        <template #expand="{ row }">
          <Loading v-if="!detail[row.claim_id]" variant="inline" />
          <div v-else>
            <div v-if="detail[row.claim_id].reason" class="reason">
              <b>裁决理由</b>：{{ detail[row.claim_id].reason }}
            </div>
            <NumericCheck :check="detail[row.claim_id].numeric_check" />
            <div v-if="detail[row.claim_id].unverifiable_label" class="reason">
              <b>无法核验的原因</b>：{{ detail[row.claim_id].unverifiable_label }}
              （{{ detail[row.claim_id].unverifiable_group === 'boundary' ? '设计边界' : '可改进' }}）——
              {{ detail[row.claim_id].unverifiable_basis }}
            </div>
            <EvidenceCite
              v-if="detail[row.claim_id].evidence"
              :title="detail[row.claim_id].evidence.title"
              :url="detail[row.claim_id].evidence.url"
              :page="detail[row.claim_id].evidence.page"
              :section="detail[row.claim_id].evidence.section"
              :text="detail[row.claim_id].evidence.text" />
            <div v-if="detail[row.claim_id].evidence_section_changes?.length" class="changes">
              <b>该章节在版本间的变化</b>（{{ detail[row.claim_id].evidence_section_changes.length }} 条）
              <ul>
                <li v-for="(change, index) in detail[row.claim_id].evidence_section_changes.slice(0, 5)"
                    :key="index">
                  {{ change.pair_key }} · 第 {{ change.right_page }} 页 · {{ DIFF_KIND_LABELS[change.diff_kind] || change.diff_kind }}
                  <template v-if="change.old_value">
                    <span class="mono">{{ change.old_value }} → {{ change.new_value }}</span>
                  </template>
                </li>
              </ul>
            </div>
            <InquiryChain v-if="detail[row.claim_id].inquiry" :inquiry="detail[row.claim_id].inquiry" />
            <RouterLink :to="`/events/${detail[row.claim_id].event_id}`" class="small">
              查看来源事件 →
            </RouterLink>
          </div>
        </template>
      </DataTable>
      <Pager :total="data.total" :limit="limit" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 6px; }
.unverifiable .groups { display: flex; gap: var(--sp-2); margin: var(--sp-2) 0; flex-wrap: wrap; }
.unverifiable .group { display: inline-flex; align-items: baseline; gap: 6px; }
.unverifiable .group .count { font-size: var(--fs-result); }
.unverifiable .group.on { border-color: var(--series-1); }
.unverifiable .subtypes { list-style: none; margin: var(--sp-2) 0 0; padding: 0; display: flex;
  flex-wrap: wrap; gap: 4px 18px; font-size: var(--fs-aux); }
.unverifiable .subtypes li { display: flex; gap: 6px; align-items: baseline; }
.link { border: none; background: none; padding: 0; min-height: 0; color: var(--series-1); cursor: pointer; }
.link:hover { text-decoration: underline; background: none; }
/* 行内的子类：设计边界用淡色，可改进用正文色——后者才是要人去管的。 */
.why-not { margin-top: 3px; color: var(--text-secondary); }
.why-not.boundary { color: var(--text-muted); }
.reason { font-size: var(--fs-body); color: var(--text-secondary); margin-bottom: 6px; }
.changes { margin-top: 10px; font-size: var(--fs-aux); color: var(--text-secondary); }
.changes ul { margin: 4px 0 0; padding-left: 18px; }
</style>
