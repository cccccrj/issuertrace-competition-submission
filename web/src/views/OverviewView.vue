<script setup>
/**
 * The landing page, rebuilt around the question a first-time reader actually
 * asks: not "how many rows are in this database" but "what does this system
 * do".
 *
 * It used to be eight counters, one paragraph, and per-company cards that were
 * not clickable — so the first screen said the platform holds data, and gave
 * no way to get from any number to the thing behind it. Now every panel drills
 * through, and the top half is organised as the brief's six tasks, one card
 * each, with the figure that shows the task ran.
 *
 * The page's original promise is kept verbatim: it does no arithmetic. Every
 * aggregate below is computed in SQL by the API, because a number computed here
 * would be a second definition of that number.
 */
import { computed, onMounted, ref } from 'vue'

import { LEVEL_META, api, companyLabel, formatDate, issueLabel, levelLabel } from '../api.js'
import EChart from '../charts/EChart.vue'
import { partBar, rankedBars, sizeHistogram, stackedTrend, tierBar } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import CountUp from '../components/CountUp.vue'
import PipelineStrip from '../components/PipelineStrip.vue'
import RateMeter from '../components/RateMeter.vue'
import StatTile from '../components/StatTile.vue'
import VerdictBar from '../components/VerdictBar.vue'

const overview = ref(null)
const pipeline = ref(null)
const trend = ref(null)
const error = ref(null)

onMounted(async () => {
  try {
    // Three requests, in parallel, and no fourth. The issue ranking used to be
    // a sequential fetch of a page of issues sorted in the browser — correct
    // only while the ledger stayed smaller than the page size, since /api/issues
    // orders by risk score rather than by event count.
    const [head, tasks, series] = await Promise.all([
      api.overview(), api.overviewPipeline(), api.overviewTrend({ months: 24 }),
    ])
    overview.value = head
    pipeline.value = tasks
    trend.value = series
  } catch (exception) {
    error.value = exception.message
  }
})

const task = (key) => pipeline.value?.tasks.find((item) => item.key === key)

/* 流水线条：采集文档，然后是六项任务各自的头条数字。数都是接口给的，这里只排顺序。 */
const stripNodes = computed(() => pipeline.value ? [
  { key: 'identify', label: '采集文档', value: pipeline.value.documents },
  ...pipeline.value.tasks.map((item) => ({ key: item.key, label: item.headline.label, value: item.headline.value })),
] : [])

/* 点流水线上的节点：滚到那张卡，卡片亮一下。亮的时长与 CSS 里的 task-flash 一致。 */
const flashed = ref('')
const cards = {}
function pick(key) {
  cards[key]?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  flashed.value = ''
  requestAnimationFrame?.(() => { flashed.value = key })
  setTimeout(() => { if (flashed.value === key) flashed.value = '' }, 1400)
}

/* 与采集页、api/routes/ingestion.py 同一张表。此前这里少了后两项，数据质量总览的第一张卡
   因此在图例里印出了英文码 excluded_listing_page。 */
const INCLUSION_LABELS = {
  included: '有效入库', excluded_document_type: '文档类型排除', excluded_no_body: '无正文',
  excluded_duplicate: '重复内容', excluded_listing_page: '列表页排除',
}
const ELEMENT_STATUS_LABELS = { normalized: '已标准化', unresolved: '未解析' }

/** Cluster sizes are a long tail — 188 pairs against one cluster of 209 — so
 *  they are banded rather than drawn as 40 columns of height one. */
const clusterBuckets = computed(() => {
  const raw = task('dedupe')?.breakdown || []
  const bands = [
    { label: '2', test: (n) => n === 2 },
    { label: '3–4', test: (n) => n >= 3 && n <= 4 },
    { label: '5–9', test: (n) => n >= 5 && n <= 9 },
    { label: '10–29', test: (n) => n >= 10 && n <= 29 },
    { label: '30+', test: (n) => n >= 30 },
  ]
  return bands.map((band) => ({
    label: band.label,
    count: raw.filter((row) => band.test(Number(row.value))).reduce((sum, row) => sum + row.count, 0),
  }))
})

const parts = (breakdown, labels) =>
  (breakdown || []).map((row) => ({
    key: row.value, label: labels[row.value] || row.value, count: row.count,
  }))

const levelParts = computed(() =>
  (task('alert')?.breakdown || [])
    .slice()
    .sort((a, b) => ['high', 'medium', 'low'].indexOf(a.value) - ['high', 'medium', 'low'].indexOf(b.value))
    .map((row) => ({
      key: row.value,
      label: levelLabel(row.value),
      count: row.count,
      colour: LEVEL_META[row.value]?.color,
    })))

const trendSeries = computed(() => (trend.value?.series || []).map((series) => ({
  key: series.key,
  label: levelLabel(series.key),
  colour: LEVEL_META[series.key]?.color,
  points: series.points,
})))

/** Two defects the real corpus exposed the moment the ranking moved into SQL.
 *
 *  The bars were labelled with the raw enum — 「SHAREHOLDER」 on the landing
 *  page — while `issueLabel` for the Chinese name had been imported all along.
 *
 *  And the ledger is per company, so 财务表现 appears once for each: the top ten
 *  contained two bars reading 「FINANCIAL」 with different heights and nothing to
 *  tell them apart. The company is appended only where a code actually repeats,
 *  because adding it to every bar would pad nine labels to disambiguate one. */
const topIssues = computed(() => {
  const rows = pipeline.value?.top_issues || []
  const seen = rows.reduce((tally, issue) => (
    { ...tally, [issue.issue_code]: (tally[issue.issue_code] || 0) + 1 }), {})
  return rows.map((issue) => ({
    label: seen[issue.issue_code] > 1
      ? `${issueLabel(issue.issue_code)} · ${companyLabel(issue.company_id)}`
      : issueLabel(issue.issue_code),
    count: issue.event_count || 0,
    id: issue.issue_id,
  }))
})

const trendRows = computed(() => (trend.value?.months || []).map((month, index) => [
  month, ...trendSeries.value.map((series) => series.points[index]),
]))

const outside = computed(() => trend.value?.out_of_window)
// 纯行情数据（事件类型层判为「一类都不贴合」）不进风险分布。命名而不是裁掉：
// 一条曲线的分母悄悄变过，比多一行脚注糟得多。
const marketData = computed(() => trend.value?.excluded_market_data || 0)
</script>

<template>
  <PageHeader title="数据质量总览"
      note="系统六项核心能力的运行概览，包括数据规模与各项任务产出。">
  </PageHeader>

  <AsyncSection
    :loading="!error && (!overview || !pipeline)"
    :error="error"
    :empty="false"
    empty-text="—"
  >
    <!-- 上半屏：这套系统在干什么 -->
    <div class="tiles">
      <StatTile label="事件" :value="overview.events" to="/events" />
      <StatTile label="已裁决主张" :value="overview.verdicts"
                note="每条非「无法核验」裁决均带页码" to="/claims" />
      <!-- /alerts 已退役，重定向会把筛选条件丢掉；直接链到筛好的事项库。 -->
      <StatTile label="未处置预警" :value="overview.alerts"
                :note="`其中高风险 ${overview.high_alerts} 条`" to="/risks?level=high&view=open" />
      <!-- 此前把「300」「人工判定 0 条」写死在这里：队列实际是 297 条，而「0 条」一有人开判就成了假话。 -->
      <StatTile label="待复核" :value="overview.review.open"
                :note="`已判 ${overview.review.decided} 条`" to="/review" />
    </div>

    <h2 class="section">命题六项任务</h2>
    <PipelineStrip :nodes="stripNodes" @pick="pick" />
    <div class="tasks">
      <section v-for="(item, index) in pipeline.tasks" :key="item.key" :id="`task-${item.key}`"
               :ref="(element) => { if (element) cards[item.key] = element }"
               class="card task" :class="{ flash: flashed === item.key }"
               :style="{ '--enter-delay': `${index * 60}ms` }">
        <span class="ordinal" aria-hidden="true">{{ String(item.ordinal).padStart(2, '0') }}</span>
        <RouterLink class="task-head" :to="item.link">
          <span class="title">{{ item.title }}</span>
          <span class="go" aria-hidden="true">→</span>
        </RouterLink>
        <div class="figure">
          <p class="headline">
            <b><CountUp :value="item.headline.value" /></b> {{ item.headline.label }}
          </p>
          <RateMeter v-if="item.rate" :rate="item.rate" class="rate" />
        </div>

        <EChart
          v-if="item.key === 'identify'"
          :builder="(tokens) => partBar(tokens, { parts: parts(item.breakdown, INCLUSION_LABELS) })"
          label="文档入库结果：有效入库、按文档类型排除、无正文三部分的占比"
          :height="80"
          :table-columns="['结果', '文档数']"
          :table-rows="parts(item.breakdown, INCLUSION_LABELS).map((p) => [p.label, p.count])" />

        <EChart
          v-else-if="item.key === 'extract'"
          :builder="(tokens) => partBar(tokens, { parts: parts(item.breakdown, ELEMENT_STATUS_LABELS) })"
          label="要素标准化情况：已标准化与未解析的占比"
          :height="80"
          :table-columns="['状态', '要素数']"
          :table-rows="parts(item.breakdown, ELEMENT_STATUS_LABELS).map((p) => [p.label, p.count])" />

        <!-- 裁决结构已有正解：固定顺序、带计数的文字图例，不必换成图库 -->
        <VerdictBar
          v-else-if="item.key === 'verify'"
          :counts="Object.fromEntries(item.breakdown.map((row) => [row.value, row.count]))"
          :total="item.breakdown.reduce((sum, row) => sum + row.count, 0)" />

        <EChart
          v-else-if="item.key === 'dedupe'"
          :builder="(tokens) => sizeHistogram(tokens, { buckets: clusterBuckets, bucketName: '簇内版本数' })"
          label="同源簇的规模分布，多数是两篇一簇，长尾里有超过三十篇的大簇"
          :height="90"
          :table-columns="['簇内版本数', '簇数']"
          :table-rows="clusterBuckets.map((b) => [b.label, b.count])" />

        <EChart
          v-else-if="item.key === 'trace'"
          :builder="(tokens) => sizeHistogram(tokens, { buckets: item.breakdown.map((row) => ({ label: row.value, count: row.count })) })"
          label="近二十四个月的事件分布"
          :height="90"
          :table-columns="['月份', '事件数']"
          :table-rows="item.breakdown.map((row) => [row.value, row.count])" />

        <EChart
          v-else
          :builder="(tokens) => partBar(tokens, { parts: levelParts })"
          label="未处置预警按风险等级的构成"
          :height="80"
          :table-columns="['等级', '预警数']"
          :table-rows="levelParts.map((p) => [p.label, p.count])" />

        <!-- 无法核验拆成两组：设计边界（本来就核不了、也不该核的）与可改进。点进去就是那一组主张。 -->
        <p v-if="item.key === 'verify' && item.unverifiable?.available" class="unverifiable-split">
          无法核验中：
          <template v-for="(group, index) in item.unverifiable.groups" :key="group.code">
            <RouterLink :to="`/claims?verdict=UNVERIFIABLE&unverifiable_group=${group.code}`">
              {{ group.label }} <b>{{ group.count.toLocaleString() }}</b>
            </RouterLink><span v-if="index < item.unverifiable.groups.length - 1" class="muted"> · </span>
          </template>
        </p>
        <p class="note">{{ item.note }}</p>
      </section>
    </div>

    <!-- 风险趋势 -->
    <section class="card">
      <h2>风险趋势</h2>
      <EChart
        v-if="trendSeries.length"
        :builder="(tokens) => stackedTrend(tokens, { periods: trend.months, series: trendSeries, valueName: '事件数' })"
        label="按月份的事件数量，按风险等级堆叠"
        :height="240"
        :table-columns="['月份', ...trendSeries.map((s) => s.label)]"
        :table-rows="trendRows" />
      <p v-if="marketData" class="footnote">
        另有 <b class="mono">{{ marketData }}</b> 条为纯行情数据（单日成交额、大宗交易明细、
        涨跌停等），事件类型层判为不属十二类中任何一类，<b>不计入风险分布，也不进预警闸门</b>；
        它们仍在事件库与传播图中。
      </p>
      <p v-if="outside && (outside.before || outside.after)" class="footnote">
        窗口取近 24 个月。另有 <b class="mono">{{ outside.before }}</b> 条早于窗口、
        <b class="mono">{{ outside.after }}</b> 条晚于今日未画出；语料时间跨
        {{ outside.earliest }} 至 {{ outside.latest }}，
        <b>后者晚于当前日期，疑为发布时间解析异常</b>，未计入趋势也未静默丢弃。
      </p>
    </section>

    <!-- 下半屏：能干活 -->
    <div class="split">
      <section class="card">
        <h2>专题热度</h2>
        <EChart
          v-if="topIssues.length"
          :builder="(tokens) => rankedBars(tokens, { items: topIssues })"
          label="事件数最多的十个专题"
          :height="260"
          :table-columns="['专题', '事件数']"
          :table-rows="topIssues.map((i) => [i.label, i.count])"
          empty="尚无专题台账。" />
      </section>

      <section class="card">
        <h2>证据可追溯</h2>
        <EChart
          v-if="pipeline.tiers.length"
          :builder="(tokens) => tierBar(tokens, { tiers: pipeline.tiers.map((t) => ({ key: t.value, label: t.value, count: t.count })) })"
          label="文档版本按来源层级的构成，S0 最权威"
          :height="80"
          :table-columns="['来源层级', '文档版本数']"
          :table-rows="pipeline.tiers.map((t) => [t.value, t.count])" />
      </section>
    </div>

    <h2 class="section">按公司</h2>
    <div class="companies">
      <RouterLink
        v-for="company in pipeline.companies"
        :key="company.company_id"
        class="card company"
        :to="`/events?company=${company.company_id}`"
      >
        <div class="company-head">
          <b>{{ companyLabel(company.company_id) }}</b>
          <span class="muted">{{ company.events }} 个事件</span>
        </div>
        <Callout v-if="!company.verifiable" tone="bad">
          未采集权威披露语料，该主体的主张无从核验。
        </Callout>
        <dl class="detail-grid">
          <dt>高风险未处置</dt><dd class="mono">{{ company.high_alerts }}</dd>
          <dt>需复核</dt><dd class="mono">{{ company.needs_review }}</dd>
          <dt>证据段落</dt><dd class="mono">{{ company.evidence_paragraphs.toLocaleString() }}</dd>
        </dl>
      </RouterLink>
    </div>

    <p v-if="overview.round" class="footnote">
      当前处理轮次 #{{ overview.round.ordinal }} ·
      参照时间 {{ formatDate(overview.round.reference_time) }} ·
      本轮新增事件 <b class="mono">{{ overview.round.new_event_count }}</b>
      <span v-if="overview.round.is_baseline">（基线轮，不判定新增）</span>
    </p>
  </AsyncSection>
</template>

<style scoped>
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr)); gap: 12px; }
.section { font-size: var(--fs-title); font-weight: 650; margin: 22px 0 10px; }
.tasks { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 12px; }
@media (min-width: 1440px) { .tasks { grid-template-columns: repeat(3, 1fr); } }
.task { position: relative; display: flex; flex-direction: column; gap: 10px; overflow: hidden;
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
  animation: task-enter 0.42s cubic-bezier(0.2, 0.7, 0.2, 1) both;
  animation-delay: var(--enter-delay, 0ms); }
/* 卡顶一道强调线：平时只露一截，悬停时伸满——告诉读者「这张卡能点进去」，又不在六张卡上
   各刷一条满宽色带。 */
.task::before { content: ''; position: absolute; left: 0; top: 0; height: 2px; width: 28px;
  background: var(--series-1); transition: width 0.28s cubic-bezier(0.2, 0.7, 0.2, 1); }
.task:hover { transform: translateY(-2px); border-color: color-mix(in srgb, var(--series-1) 40%, var(--border));
  box-shadow: 0 6px 18px color-mix(in srgb, var(--text-primary) 9%, transparent); }
.task:hover::before { width: 100%; }
.task.flash { animation: task-flash 1.4s ease-out; }
@keyframes task-enter { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
@keyframes task-flash {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--series-1) 45%, transparent); }
  100% { box-shadow: 0 0 0 10px transparent; }
}
.task-head { display: flex; align-items: baseline; gap: 8px; color: var(--text-primary); padding-right: 40px; }
.task-head:hover { text-decoration: none; }
.task-head:hover .title { text-decoration: underline; }
/* 序号退到右上角做水印：大、淡，不抢标题和头条数字。 */
.ordinal { position: absolute; right: 14px; top: 8px; font-size: var(--fs-display); font-weight: 700;
  letter-spacing: -0.04em; line-height: 1; color: var(--text-muted); opacity: 0.28; pointer-events: none; }
.title { font-weight: 600; font-size: var(--fs-body); }
.go { margin-left: 4px; color: var(--text-muted); transition: transform 0.18s ease, color 0.18s ease; }
.task:hover .go { transform: translateX(3px); color: var(--series-1); }
.figure { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 16px; align-items: end; }
.headline { margin: 0; font-size: var(--fs-aux); color: var(--text-secondary); white-space: nowrap; }
/* 头条数字用比例数字（不加 .mono）：等宽数字在大字号下显得松。 */
.headline b { font-size: var(--fs-display); font-weight: 640; letter-spacing: -0.02em;
  color: var(--text-primary); margin-right: 4px; }
.rate { padding-bottom: 4px; }
.unverifiable-split { margin: 0; font-size: var(--fs-aux); color: var(--text-secondary); }
.unverifiable-split b { color: var(--text-primary); }
.note { margin: 0; font-size: var(--fs-note); color: var(--text-muted); }
.split { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); gap: 12px; margin-top: 12px; }
/* 整幅的独立小节自己和上文拉开距离。 */
section.card { margin-top: 12px; }
/* 网格里的卡片不需要——`gap` 已经在管间距了，而这里的外边距只会落在第一张卡
   上：把它清零的那条规则是相邻兄弟选择器（`.card + .card`，两个类，优先级还
   高过 `section.card`），而第一个子元素没有前兄弟，选不中。
   六项任务的卡①因此比卡②③矮了 12px、底边却对齐；第二行整齐，只是因为那一行
   每张卡前面都有兄弟。 */
.tasks > section.card,
.split > section.card,
.companies > section.card { margin-top: 0; }
.companies { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 12px; }
.company { display: block; color: inherit; }
.company:hover { text-decoration: none; border-color: var(--series-1); }
.company-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 10px; }
@media (prefers-reduced-motion: reduce) {
  .task, .task.flash { animation: none; transition: none; }
  .task:hover { transform: none; }
  .task::before, .go { transition: none; }
}
</style>
