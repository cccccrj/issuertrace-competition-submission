<script setup>
/**
 * 风险事项库 — 跨来源聚合后的业务对象，不是一篇文章，也不是一条主张。
 *
 * 一条「风险事项」就是处理层按（公司，议题，7 天发布窗口）合并出来的那一条预警：
 * 库里 277 条，合并了 388 个事件。这里和事件库（1,907 条）刻意用两个名字——两个
 * 页面共用一个名字，读者第一次对不上数就不会再相信任何一个数。
 *
 * 默认表格里没有原文哈希、簇 ID、模型版本：它们没被删掉，收在详情页的处理记录里
 * （规格 §4.3）。一张十列的表如果三列是给工程师看的，剩下七列就不会被读完。
 */
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  CREDIBILITY_SHORT, DISPOSITION_STATUS_META, ISSUE_LABELS, api, companyLabel, formatDate,
  issueLabel,
} from '../api.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import LevelBadge from '../components/LevelBadge.vue'
import Pager from '../components/Pager.vue'
import { debouncedField } from '../debounce.js'
import { fromQuery } from '../deeplink.js'

const route = useRoute()
const BLANK = { company: '', issue: '', level: '', credibility: '', status: '', conclusion: '', search: '' }
const filters = ref(fromQuery(BLANK, route.query))
const searchDraft = debouncedField(filters)
const offset = ref(0)
const limit = 25
const data = ref(null)
const loading = ref(true)
const error = ref('')
const expanded = ref(null)

/* 预置视图只是几组筛选条件的简写，筛选本身在服务端，所以视图入口和手动筛出来的是
   同一个东西，不是几条各自实现一遍的查询。

   来源有两处：路由的 `meta.preset`（页内那排视图切换），和地址栏的 `?view=`（别人
   发过来的深链）。地址栏优先——一条链接点开后不按链接说的显示，是最难排查的那种
   缺陷。 */
const VIEWS = {
  anomaly: { title: '核验异常', note: '证据冲突、高影响低可信、仅转述，或合并事件里有待人工复核的；已归档的不算。' },
  handling: { title: '处理中', note: '核查中、待上级复核、退回补证——已经有人接手、还没有定论的。' },
  open: { title: '未归档', note: '尚未归档的全部事项。' },
  archived: { title: '已归档', note: '本轮处置已结束的事项，保留以备回查。' },
}
const view = computed(() => route.query.view || route.meta?.preset?.view || '')
const preset = computed(() => VIEWS[view.value] || null)
/* 首页四张异常卡片各自点进来时带着 `?anomaly=`，只看那一类。 */
const anomaly = computed(() => route.query.anomaly || '')

/* 核验异常与已归档此前各占侧栏一格，其实是同一张表的几种筛法。入口收回到本页：
   六个视图在一排上，读者看得见「全部」之外还有哪几种看法。地址不变，旧链接照常打开。
   「待研判」「持续跟踪」就是状态筛选，页签与筛选器里那个下拉框是同一个条件。 */
const VIEW_TABS = [
  { to: '/risks', label: '全部', view: '', status: '' },
  { to: '/risks?status=pending', label: '待研判', view: '', status: 'pending' },
  { to: '/risks/anomalies', label: '核验异常', view: 'anomaly', status: '' },
  { to: '/risks?view=handling', label: '处理中', view: 'handling', status: '' },
  { to: '/risks?status=tracking', label: '持续跟踪', view: '', status: 'tracking' },
  { to: '/risks/archived', label: '已归档', view: 'archived', status: '' },
]
const onTab = (tab) => view.value === tab.view && (tab.view || filters.value.status === tab.status)

/* 页签是链接，换的是地址栏；同一个组件不会重建，所以地址一变就把筛选重新读一遍。 */
watch(() => route.query, (query) => { filters.value = fromQuery(BLANK, query) })

const LEVEL_OPTIONS = { high: '高风险', medium: '中风险', low: '一般关注' }
const CREDIBILITY_OPTIONS = { high: '可信度高', medium: '可信度中', low: '可信度低' }
const STATUS_OPTIONS = Object.fromEntries(
  Object.entries(DISPOSITION_STATUS_META).map(([value, meta]) => [value, meta.label]))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = { ...filters.value, limit, offset: offset.value }
    if (view.value) params.view = view.value
    if (anomaly.value) params.anomaly = anomaly.value
    data.value = await api.risks(params)
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

watch(filters, () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)
watch([view, anomaly], () => { offset.value = 0; load() })
load()

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() {
  filters.value = { ...BLANK }
}

const COLUMNS = [
  { key: 'risk_level', title: '风险等级', slot: 'level', nowrap: true, width: '104px' },
  { key: 'headline', title: '事项', slot: 'headline' },
  { key: 'issue_code', title: '专题', slot: 'issue', nowrap: true, width: '96px' },
  { key: 'credibility', title: '事件可信度', slot: 'credibility', nowrap: true, width: '96px' },
  { key: 'heat', title: '传播热度', slot: 'heat', nowrap: true, width: '104px' },
  { key: 'status', title: '处置状态', slot: 'status', nowrap: true, width: '96px' },
  { key: 'owner', title: '责任人', slot: 'owner', nowrap: true, width: '78px' },
  { key: 'latest', title: '最新进展', slot: 'latest', nowrap: true, width: '108px' },
  { key: 'go', title: '操作', slot: 'go', nowrap: true, width: '92px' },
]
const CONCLUSION_OPTIONS = computed(() => Object.fromEntries(
  Object.entries(data.value?.conclusions || {}).map(([value, meta]) => [value, meta.label])))

</script>

<template>
  <!-- note 是字面量而不是绑定值：`conventions.test.js` 读源码文本来保证每一页都有
       那一句、且不超过 40 字，一个绑定表达式它读不出来。三个预置视图各自那句话
       放在下面的 preset-note 里，它说的是「这个视图筛掉了什么」，与导语不是一件事。 -->
  <PageHeader title="风险事项库"
      note="已识别的公司风险事项及其处置进度，按紧急度排序。">
    <template #basis>
      <p>
        一条事项是处理层按（公司，专题，7 天发布窗口）合并出来的一组事件，不是一篇报道。
        因此此处条数（{{ data?.total ?? '—' }}）少于事件库中的事件数，仅超过预警线的事件组合才形成事项。
      </p>
      <p>
        风险等级是平台内部的排序规则，用于决定先处理哪一条，不是监管的统一分级。
      </p>
    </template>
  </PageHeader>

  <nav class="view-tabs" aria-label="事项视图">
    <RouterLink v-for="tab in VIEW_TABS" :key="tab.to" :to="tab.to"
                :class="{ on: onTab(tab) }" :aria-current="onTab(tab) ? 'page' : undefined">
      {{ tab.label }}
    </RouterLink>
  </nav>
  <p v-if="preset" class="page-note preset-note">
    {{ preset.note }}
    <template v-if="anomaly && data?.anomaly_kinds?.[anomaly]">
      现在只看「{{ data.anomaly_kinds[anomaly].label }}」一类。
      <RouterLink to="/risks/anomalies">看全部异常 →</RouterLink>
    </template>
  </p>

  <FilterBar :active="active" @reset="reset">
    <CompanySelect v-model="filters.company" />
    <EnumSelect v-model="filters.issue" :labels="ISSUE_LABELS" all-label="全部专题" />
    <EnumSelect v-model="filters.level" :labels="LEVEL_OPTIONS" all-label="全部风险等级" />
    <EnumSelect v-model="filters.credibility" :labels="CREDIBILITY_OPTIONS" all-label="全部可信度" />
    <EnumSelect v-model="filters.status" :labels="STATUS_OPTIONS" all-label="全部处置状态" />
    <EnumSelect v-model="filters.conclusion" :labels="CONCLUSION_OPTIONS" all-label="全部人工结论" />
    <input v-model="searchDraft" type="search" placeholder="搜索事项标题或摘要…" aria-label="搜索事项标题或摘要" />
    <template #end>
      <span v-if="data" class="muted small">共 {{ data.total.toLocaleString() }} 条事项</span>
    </template>
  </FilterBar>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    empty-text="没有符合条件的风险事项。清除筛选可以看全部事项。"
  >
    <div class="card">
      <DataTable
        :columns="COLUMNS"
        :rows="data.items"
        row-key="alert_id"
        expandable
        v-model:expanded="expanded"
      >
        <template #level="{ row }">
          <LevelBadge :level="row.risk_level" :score="row.adjusted_risk_score" />
        </template>
        <template #headline="{ row }">
          <div class="headline">{{ row.headline }}</div>
          <div class="muted small">
            <RouterLink :to="`/companies/${row.company_id}`" class="company">{{ companyLabel(row.company_id) }}</RouterLink>
            <span v-if="row.lead_title"> · 首发报道：{{ row.lead_title }}</span>
          </div>
        </template>
        <template #issue="{ row }">
          <span v-if="row.issue_code" class="tag">{{ issueLabel(row.issue_code) }}</span>
          <span v-else class="muted small">未归专题</span>
        </template>
        <template #credibility="{ row }">
          <span class="mono">{{ row.credibility?.toFixed?.(2) ?? '—' }}</span>
          <span class="muted small"> {{ CREDIBILITY_SHORT[row.credibility_label] || '—' }}</span>
        </template>
        <template #heat="{ row }">
          <span class="mono">{{ row.heat.event_count }}</span>
          <span class="muted small"> 篇 ·
            {{ row.heat.independent_sources ?? '—' }} 信源</span>
        </template>
        <template #status="{ row }">
          <!-- 颜色、图标、文字同时在场：状态不靠颜色单独承载。 -->
          <span class="state" :style="{ color: DISPOSITION_STATUS_META[row.disposition.status]?.color }">
            <span aria-hidden="true">{{ DISPOSITION_STATUS_META[row.disposition.status]?.icon }}</span>
            {{ row.disposition.status_label }}
          </span>
          <span v-if="row.disposition.conclusion_label" class="conclusion small"
                :class="{ fp: row.disposition.false_positive }">{{ row.disposition.conclusion_label }}</span>
        </template>
        <template #go="{ row }">
          <RouterLink :to="`/risks/${row.alert_id}`">查看事项 →</RouterLink>
        </template>
        <template #owner="{ row }">
          <span v-if="row.disposition.owner">{{ row.disposition.owner }}</span>
          <span v-else class="muted small">未指派</span>
        </template>
        <template #latest="{ row }">
          <span class="mono">{{ formatDate(row.latest_progress) }}</span>
        </template>

        <template #expand="{ row }">
          <div class="peek">
            <p class="summary">{{ row.summary }}</p>
            <div class="reasons">
              <span v-for="reason in row.reasons" :key="reason.code" class="tag">
                {{ reason.label }}
              </span>
            </div>
            <p v-if="row.disposition.recommended_label" class="muted small">
              系统建议：{{ row.disposition.recommended_label }}
              <span v-if="row.disposition.diverged"> · 人工选择与建议不同</span>
            </p>
            <RouterLink class="open" :to="`/risks/${row.alert_id}`">进入研判 →</RouterLink>
          </div>
        </template>
      </DataTable>
      <Pager :total="data.total" :limit="limit" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.view-tabs { display: flex; gap: 4px; margin: calc(-1 * var(--sp-2)) 0 var(--sp-4);
  border-bottom: 1px solid var(--border); }
.view-tabs a { padding: 6px 12px; margin-bottom: -1px; color: var(--text-secondary);
  border-bottom: 2px solid transparent; }
.view-tabs a:hover { text-decoration: none; color: var(--text-primary); }
.view-tabs a.on { color: var(--text-primary); font-weight: 600; border-bottom-color: var(--series-1); }
.preset-note { margin-top: calc(-1 * var(--sp-2)); }
.headline { font-weight: 600; }
.state { white-space: nowrap; }
.conclusion { display: block; color: var(--text-secondary); }
.conclusion.fp { color: var(--text-muted); text-decoration: line-through; }
.peek { padding: 2px 0; }
.summary { margin: 0 0 8px; max-width: var(--measure); color: var(--text-secondary); }
.reasons { margin-bottom: 8px; }
.open { display: inline-block; margin-top: 6px; font-weight: 600; }
</style>
