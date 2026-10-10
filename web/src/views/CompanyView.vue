<script setup>
/**
 * 公司档案 — 这家公司现在有哪些事、风险往哪走、最近正式说了什么、在哪几个专题里、跟谁有关。
 *
 * 只有详情页，没有列表页：库里两家公司，两行的列表是空壳，还要多点一次才进得去。入口是
 * 事项库、事件库、研判档案里那个公司名。
 *
 * **这个文件里不写任何一家公司。** 名字、全称、股票代码都由接口从配置带来；写死一个，
 * 「加第三家公司不改代码」就成了假话（`tests/test_migration` 的扫描覆盖到前端）。
 */
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { LEVEL_META, api, formatDate, levelLabel } from '../api.js'
import EChart from '../charts/EChart.vue'
import { stackedTrend } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import DataTable from '../components/ui/DataTable.vue'
import LevelBadge from '../components/LevelBadge.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import StatTile from '../components/StatTile.vue'

const route = useRoute()
const data = ref(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.company(route.params.id)
  } catch (exception) {
    error.value = exception.message
    data.value = null
  } finally {
    loading.value = false
  }
}
watch(() => route.params.id, load, { immediate: true })

const TILES = computed(() => {
  const d = data.value
  if (!d) return []
  const id = encodeURIComponent(d.company_id)
  return [
    { label: '未结事项', value: d.open.total, note: '没归档的都算，含待研判', to: `/risks?company=${id}` },
    { label: '高风险未结', value: d.open.high, note: '高风险且未归档', to: `/risks?company=${id}&level=high` },
    { label: '核查逾期', value: d.open.overdue, note: '专项核查超过截止日', to: '/investigations' },
    { label: '权威披露', value: d.disclosures.total, note: '交易所、监管与公司正式文件' },
  ]
})

/** 接口只给有事项的月份；图上要一条连续的时间轴，中间空着的月份补 0。 */
const months = computed(() => {
  const rows = data.value?.trend || []
  if (!rows.length) return []
  const [first, last] = [rows[0].month, rows[rows.length - 1].month]
  const out = []
  let [y, m] = first.split('-').map(Number)
  const [ly, lm] = last.split('-').map(Number)
  while (y < ly || (y === ly && m <= lm)) {
    out.push(`${y}-${String(m).padStart(2, '0')}`)
    m += 1
    if (m > 12) { m = 1; y += 1 }
  }
  return out
})
const trendSeries = computed(() => {
  const byMonth = Object.fromEntries((data.value?.trend || []).map((row) => [row.month, row]))
  return ['high', 'medium', 'low'].map((key) => ({
    key, label: levelLabel(key), colour: LEVEL_META[key]?.color,
    points: months.value.map((month) => byMonth[month]?.[key] || 0),
  }))
})
const trendRows = computed(() => months.value.map((month, index) => [
  month, ...trendSeries.value.map((series) => series.points[index]),
]))

/** 事项标题以「公司名｜」开头——在这家公司自己的档案页上，八行都先读一遍公司名，
 *  能区分这一条的那半截被挤到了右边。只剥开头这一个前缀，其余原样。 */
const headline = (text) => {
  const prefix = `${data.value?.name}｜`
  return text?.startsWith(prefix) ? text.slice(prefix.length) : text
}

const OPEN_COLUMNS = [
  { key: 'risk_level', title: '风险等级', slot: 'level', nowrap: true, width: '104px' },
  { key: 'headline', title: '事项', slot: 'headline' },
  { key: 'status', title: '处置状态', slot: 'status', nowrap: true, width: '96px' },
  { key: 'latest', title: '最新进展', slot: 'latest', nowrap: true, width: '108px' },
]
</script>

<template>
  <PageHeader :title="data?.name || '公司档案'" note="公司当前风险事项、风险走势及近期权威披露。" />

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <p class="muted small ident">
      {{ data.canonical_name }}
      <template v-for="code in data.stock_codes" :key="code.code">
        · {{ code.exchange }} <span class="mono">{{ code.code }}</span>
      </template>
    </p>

    <div class="tiles">
      <StatTile v-for="tile in TILES" :key="tile.label"
                :label="tile.label" :value="tile.value" :note="tile.note" :to="tile.to" />
    </div>

    <section class="card">
      <h2 class="section">风险趋势</h2>
      <EChart
        v-if="months.length"
        :builder="(tokens) => stackedTrend(tokens, { periods: months, series: trendSeries })"
        label="这家公司的风险事项按月份计数，按风险等级堆叠"
        :height="180"
        :table-columns="['月份', ...trendSeries.map((series) => series.label)]"
        :table-rows="trendRows" />
      <p v-else class="muted small">还没有风险事项。</p>
    </section>

    <section class="card">
      <h2 class="section">未结事项</h2>
      <p v-if="!data.open.items.length" class="muted small">没有未结的风险事项。</p>
      <template v-else>
        <DataTable :columns="OPEN_COLUMNS" :rows="data.open.items" row-key="alert_id">
          <template #level="{ row }">
            <LevelBadge :level="row.risk_level" :score="row.adjusted_risk_score" />
          </template>
          <template #headline="{ row }">
            <RouterLink :to="`/risks/${row.alert_id}`">{{ headline(row.headline) }}</RouterLink>
          </template>
          <template #status="{ row }"><span class="small">{{ row.disposition.status_label }}</span></template>
          <template #latest="{ row }"><span class="mono small">{{ formatDate(row.latest_progress) }}</span></template>
        </DataTable>
        <RouterLink v-if="data.open.total > data.open.items.length" class="more small"
                    :to="`/risks?company=${encodeURIComponent(data.company_id)}`">
          在风险事项库查看全部 {{ data.open.total }} 条 →
        </RouterLink>
      </template>
    </section>

    <div class="split">
      <section class="card">
        <h2 class="section">重点专题</h2>
        <p v-if="!data.issues.length" class="muted small">还没有归入专题的事件。</p>
        <ul v-else class="plain">
          <li v-for="issue in data.issues" :key="issue.issue_id">
            <RouterLink :to="`/issues/${issue.issue_id}`">{{ issue.issue_label || issue.issue_code }}</RouterLink>
            <span class="muted small">
              · {{ levelLabel(issue.risk_level) }} · {{ issue.event_count }} 个事件 ·
              {{ issue.independent_source_count ?? '—' }} 个独立信源
            </span>
          </li>
        </ul>
      </section>

      <section class="card">
        <h2 class="section">关联主体</h2>
        <p class="page-note">这家公司的事件里反复出现的其他机构，按出现在几个事件里计。</p>
        <p v-if="!data.related.length" class="muted small">还没有解析出的关联机构。</p>
        <ul v-else class="chips">
          <li v-for="org in data.related" :key="org.org_id" class="tag">
            {{ org.name }}<span v-if="org.kind_label" class="muted"> · {{ org.kind_label }}</span>
            <span class="mono">{{ org.events }}</span>
          </li>
        </ul>
      </section>
    </div>

    <section class="card">
      <h2 class="section">最新权威披露</h2>
      <p v-if="!data.disclosures.items.length" class="muted small">还没有采集到权威披露。</p>
      <ul v-else class="plain">
        <li v-for="doc in data.disclosures.items" :key="doc.version_id">
          <span class="mono small">{{ formatDate(doc.published_at) }}</span>
          <a v-if="doc.canonical_url" :href="doc.canonical_url" target="_blank" rel="noopener">{{ doc.title }}</a>
          <span v-else>{{ doc.title }}</span>
          <span class="muted small"> · {{ doc.source_name || '—' }}</span>
        </li>
      </ul>
    </section>
  </AsyncSection>
</template>

<style scoped>
.ident { margin: 0 0 var(--sp-3); }
.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-3); margin-bottom: var(--sp-3); }
section.card { margin-bottom: var(--sp-3); }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
.plain { list-style: none; margin: 0; padding: 0; }
.plain li { padding: 6px 0; border-top: 1px solid var(--gridline); display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
.plain li:first-child { border-top: 0; }
.chips { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
/* 模板里两个 span 之间的换行会被 Vue 的空白压缩吃掉，计数直接贴在「外国政府」后面。 */
.chips .mono { margin-left: 6px; }
.more { display: inline-block; margin-top: var(--sp-2); }
@media (max-width: 900px) { .split, .tiles { grid-template-columns: 1fr 1fr; } }
@media (max-width: 600px) { .split { grid-template-columns: 1fr; } }
</style>
