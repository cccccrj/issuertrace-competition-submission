<script setup>
/**
 * 风险预警中心 —— 默认首页。
 *
 * 首屏只回答四个问题：今天有哪些事项要我处理、哪些最紧急、哪些证据不足或有冲突、
 * 我的下一步是什么。所以这里**没有**文档总数、主张总数、要素标准化率这类数字——
 * 它们没被删掉，搬到了「数据与质量 · 数据质量总览」。驾驶舱回答的是「库里有多少东西」，
 * 那是个好问题，但不是一个金融人员早上打开系统时的第一个问题。
 *
 * 这一条由测试钉住：本文件不得出现 `overview(` / `overviewPipeline`。
 *
 * 页面自己不做任何算术，与原驾驶舱同一条纪律：每个聚合都由 SQL 算好送来，
 * 在这里再算一遍就是同一个数的第二份定义。
 */
import { computed, ref, watch } from 'vue'

import { LEVEL_META, api, companyLabel, formatDate, levelLabel } from '../api.js'
import EChart from '../charts/EChart.vue'
import { stackedTrend } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import DataTable from '../components/ui/DataTable.vue'
import LevelBadge from '../components/LevelBadge.vue'
import StatTile from '../components/StatTile.vue'

const summary = ref(null)
const queue = ref(null)
const trend = ref(null)
const loading = ref(true)
const error = ref('')
/* 趋势图的公司筛选只管这一张图：卡片与队列回答的是「今天全组要处理什么」，按公司切开
   就不是这个问题了。时间范围不给 7 天 / 30 天——语料是静态库，按天切出来多半是空图。 */
const trendCompany = ref('')

async function loadTrend() {
  try {
    trend.value = await api.overviewTrend({ months: 12, company: trendCompany.value || undefined })
  } catch {
    trend.value = null
  }
}
watch(trendCompany, loadTrend)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [head, rows, series] = await Promise.all([
      api.riskSummary(),
      api.risks({ view: 'open', limit: 6 }),
      api.overviewTrend({ months: 12 }),
    ])
    summary.value = head
    queue.value = rows
    trend.value = series
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}
load()

/** 四张卡。数字点下去进入已经筛好的事项列表——一个点不动的数字等于没给出口。
 *
 *  「待研判」没有改成「待我研判」：库里的待研判事项此前没有一条有负责人，按负责人
 *  筛出来对所有人都是 0。它仍是全组的队列，旁边说出其中几条已经分派给你。 */
const TILES = computed(() => {
  const counts = summary.value?.indicators || {}
  return [
    { label: '待研判', value: counts.pending ?? 0,
      note: counts.mine_pending ? `其中 ${counts.mine_pending} 条已分派给你` : '尚未完成人工结论',
      to: '/risks?status=pending' },
    { label: '高风险未结', value: counts.high_open ?? 0, note: '高风险且尚未归档',
      to: '/risks?level=high&view=open' },
    { label: '异常待核查', value: counts.anomaly ?? 0, note: '证据冲突 / 仅转述 / 高影响低证据',
      to: '/risks/anomalies' },
    { label: '待我处理', value: counts.my_tasks ?? 0, note: '负责人是你、还没归档',
      to: '/todo' },
  ]
})

const trendSeries = computed(() => (trend.value?.series || []).map((series) => ({
  key: series.key,
  label: levelLabel(series.key),
  colour: LEVEL_META[series.key]?.color,
  points: series.points,
})))
const trendRows = computed(() => (trend.value?.months || []).map((month, index) => [
  month, ...trendSeries.value.map((series) => series.points[index]),
]))

const QUEUE_COLUMNS = [
  { key: 'risk_level', title: '等级', slot: 'level', nowrap: true, width: '104px' },
  { key: 'headline', title: '风险事项', slot: 'headline' },
  { key: 'credibility', title: '可信度', slot: 'credibility', nowrap: true, width: '74px' },
  { key: 'status', title: '状态', slot: 'status', nowrap: true, width: '78px' },
  { key: 'owner', title: '负责人', slot: 'owner', nowrap: true, width: '72px' },
  { key: 'latest', title: '最新进展', slot: 'latest', nowrap: true, width: '100px' },
  { key: 'go', title: '操作', slot: 'go', nowrap: true, width: '92px' },
]
</script>

<template>
  <PageHeader title="风险预警中心"
      note="今日待研判、高风险及证据异常事项的处理优先级。">
    <template #basis>
      <p>
        本页仅统计已超过预警线的风险事项。采集量、抽取率与标准化率等系统运行指标
        请见「数据与质量 · 数据质量总览」。
      </p>
      <p>
        风险等级是平台内部的排序规则，用于决定先处理哪一条，不是监管的统一分级。
      </p>
    </template>
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="!summary"
                empty-text="暂未生成风险事项。请先在「数据与质量 · 多源信息采集与事件转化」确认采集是否已运行。">
    <!-- 工作提示条：一句话说清今天的量，右边直接给出口。 -->
    <Callout tone="info">
      今日共 <b>{{ summary.indicators.pending }}</b> 项待研判，其中
      <b>{{ summary.indicators.high_open }}</b> 项高风险、
      <b>{{ summary.indicators.anomaly }}</b> 项证据异常待核查。
      <RouterLink class="lead-link" to="/risks?status=pending">查看待研判 →</RouterLink>
    </Callout>

    <div class="tiles">
      <StatTile v-for="tile in TILES" :key="tile.label"
                :label="tile.label" :value="tile.value" :note="tile.note" :to="tile.to" />
    </div>

    <!-- 趋势在队列之前：先看这个月是升是降，再决定今天从哪一条开始点。图高压到
         180——它排在待办队列上面，每多一像素都是把「先点哪一条」往下推一像素。 -->
    <section class="card">
      <div class="trend-head">
        <h2 class="section">风险趋势</h2>
        <CompanySelect v-model="trendCompany" />
      </div>
      <EChart
        v-if="trendSeries.length"
        :builder="(tokens) => stackedTrend(tokens, { periods: trend.months, series: trendSeries })"
        label="按月份的事件数量，按风险等级堆叠"
        :height="180"
        :table-columns="['月份', ...trendSeries.map((series) => series.label)]"
        :table-rows="trendRows" />
      <p v-else class="empty">近 12 个月没有可画的事件。</p>
    </section>

    <div class="split">
      <section class="card queue">
        <h2 class="section">待处置风险事项</h2>
        <p class="page-note">高风险且待研判的排在最前，其余按修正后风险分。</p>
        <DataTable :columns="QUEUE_COLUMNS" :rows="queue.items" row-key="alert_id">
          <template #level="{ row }">
            <LevelBadge :level="row.risk_level" :score="row.adjusted_risk_score" />
          </template>
          <template #headline="{ row }">
            <div class="headline">{{ row.headline }}</div>
            <div class="muted small">
              {{ companyLabel(row.company_id) }} ·
              {{ row.reasons.map((reason) => reason.label).join('；') }}
            </div>
          </template>
          <template #credibility="{ row }">
            <span class="mono">{{ row.credibility?.toFixed?.(2) ?? '—' }}</span>
          </template>
          <template #status="{ row }">{{ row.disposition.status_label }}</template>
          <template #owner="{ row }">
            <span v-if="row.disposition.owner">{{ row.disposition.owner }}</span>
            <span v-else class="muted small">未认领</span>
          </template>
          <template #latest="{ row }">
            <span class="mono">{{ formatDate(row.latest_progress) }}</span>
          </template>
          <template #go="{ row }">
            <RouterLink :to="`/risks/${row.alert_id}`">进入研判 →</RouterLink>
          </template>
        </DataTable>
        <RouterLink class="more" to="/risks">查看全部 {{ summary.total }} 条事项 →</RouterLink>
      </section>

      <!-- 右栏装两块：异常入口与公司动态都是「几行数字」，窄栏正好；此前公司动态
           独占下面一整行，两家公司撑一张通栏卡，而右栏从队列的第三行起就空着。 -->
      <div class="rail">
        <section class="card anomalies">
          <h2 class="section">异常待核查</h2>
          <p class="page-note">以下四类问题出在证据本身，不按风险分排序。</p>
          <!-- 每一类点进去只看这一类：此前四类都链到同一个「核验异常」全集，点哪一格都一样。
               数与列表用的是服务端同一句 SQL，点之前的数就是点之后的条数。 -->
          <RouterLink v-for="row in summary.anomalies" :key="row.key"
                      class="anomaly" :to="`/risks/anomalies?anomaly=${row.key}`">
            <span class="anomaly-count mono">{{ row.count }}</span>
            <span class="anomaly-body">
              <span class="anomaly-label">{{ row.label }}</span>
              <span class="muted small">{{ row.detail }}</span>
              <span class="anomaly-action small">{{ row.action }} →</span>
            </span>
          </RouterLink>
        </section>

        <section class="card">
          <h2 class="section">重点公司动态</h2>
          <RouterLink v-for="row in summary.companies" :key="row.company_id"
                      class="company" :to="`/risks?company=${row.company_id}`">
            <span class="company-name">{{ companyLabel(row.company_id) }}</span>
            <span class="company-figures muted small">
              未结 <b class="mono">{{ row.open }}</b> · 高风险 <b class="mono">{{ row.high }}</b>
              · {{ formatDate(row.latest_at) }}
            </span>
          </RouterLink>
        </section>
      </div>
    </div>

  </AsyncSection>
</template>

<style scoped>
.lead-link { margin-left: var(--sp-2); font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-3);
  margin-bottom: var(--sp-3); }
/* 左 8 右 4：待处置的事项占主视野，异常分类是它旁边的一列入口而不是另一屏。 */
.split { display: grid; grid-template-columns: 2fr 1fr; gap: var(--sp-3);
  align-items: start; margin-bottom: var(--sp-3); }
.headline { font-weight: 600; }
/* 预警理由截成一行：它是标题的旁注，不是第二个标题。放开写时每行占三行高，
   六条队列就长到一屏半，而首页要回答的是「先点哪一条」，不是「读完每一条」。 */
.queue .muted.small { display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;
  overflow: hidden; }
.rail { display: flex; flex-direction: column; gap: var(--sp-3); }
.company { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-2) 0;
  border-top: 1px solid var(--gridline); text-decoration: none; }
.company:hover { background: var(--hover-wash); }
.company-name { color: var(--text-primary); font-weight: 600; }
.more { display: inline-block; margin-top: var(--sp-2); }
.anomaly { display: flex; gap: var(--sp-3); align-items: baseline; padding: var(--sp-2) 0;
  border-top: 1px solid var(--gridline); text-decoration: none; }
.anomaly:hover { background: var(--hover-wash); }
.anomaly-count { font-size: var(--fs-result); min-width: 2.5em; text-align: right; }
.anomaly-body { display: flex; flex-direction: column; }
.anomaly-label { color: var(--text-primary); }
.anomaly-action { color: var(--series-1); font-weight: 600; margin-top: 2px; }
.trend-head { display: flex; justify-content: space-between; align-items: center; gap: var(--sp-3); }

@media (max-width: 1100px) {
  .tiles { grid-template-columns: repeat(2, 1fr); }
  .split { grid-template-columns: 1fr; }
}
</style>
