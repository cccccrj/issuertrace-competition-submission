<script setup>
/* 判据是写给模型的承重文本，里面带着字面 Markdown 星号（`判据是**正在发行…**`）。
   它经 title 属性渲染时，读者悬停看到的就是带星号的原文——接口那份不动，只在
   显示这一层剥掉。conventions.test.js 那条「模板里没有字面 Markdown」扫的是模板
   文本，扫不到运行时数据，所以这个缺陷一直没被发现。 */
const unmarked = (text) => (text || '').replace(/\*\*/g, '')

/**
 * 主数据与词表 — the standardisation vocabulary, and how much of it is alive.
 *
 * Migrated onto the shared layer. The page's own point is that a code sitting
 * unused and a code used nine hundred times look identical in the config file,
 * so the usage counts are what it exists to show — and a column of numbers is a
 * poor way to see that a whole vocabulary is nearly dead. Each table now has a
 * ranked bar beside it, and the unused share is stated in words.
 */
import { computed, ref } from 'vue'

import { api, companyLabel } from '../api.js'
import EChart from '../charts/EChart.vue'
import { rankedBars } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import DataTable from '../components/ui/DataTable.vue'
import PageHeader from '../components/ui/PageHeader.vue'

const vocabularies = ref(null)
const companies = ref(null)
const financials = ref(null)
const loading = ref(true)
const error = ref('')

async function load() {
  try {
    const [terms, scope, books] = await Promise.all([
      api.vocabularies(), api.companies(), api.financials(),
    ])
    vocabularies.value = terms
    companies.value = scope
    financials.value = books
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}
load()

/** 主要会计数据，按公司分组。
 *
 *  口径两条，都写在页面上而不是只写在这里：期间取自报告标题而不是发布日期
 *  （年报次年三月才披露），每个数字都带着它出自哪一份文件。
 *
 *  只取本期一列。每张表还有一到两列上期数，把它们归到某个期间要先相信列的
 *  顺序——这是单份文件内部验证不了的假设，所以不做；上期数本来也就是更早
 *  那份报告的本期数。 */
const PERIOD_TYPE_LABELS = { annual: '年度', interim: '半年度', quarterly: '季度' }

/** 一列数字要能上下对齐着比，所以小数位固定两位——`maximumFractionDigits`
 *  单用会把 1,503.10 印成 1,503.1，紧挨着 776.05，读起来像两种精度。 */
function money(value) {
  if (value === undefined || value === null) return '—'
  return `${(value / 1e8).toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })} 亿`
}
function plain(value, digits = 2) {
  return value === undefined || value === null ? '—' : value.toFixed(digits)
}
const financialRows = computed(() => (financials.value?.items || []).map((row) => ({
  ...row, key: `${row.company_id}:${row.period}`,
})))
/** Codes that have never been used, per vocabulary. A vocabulary where most
 *  entries are unused is either aspirational or wrong for this corpus, and both
 *  are worth noticing before somebody adds a fifth entry to it. */
const FIN_COLUMNS = [
  { key: 'company_id', title: '公司', slot: 'company', nowrap: true },
  { key: 'period', title: '报告期', slot: 'period', nowrap: true, width: '110px' },
  { key: 'revenue', title: '营业收入', slot: 'revenue', nowrap: true },
  { key: 'profit', title: '归母净利润', slot: 'profit', nowrap: true },
  { key: 'cash', title: '经营现金流', slot: 'cash', nowrap: true },
  { key: 'assets', title: '总资产', slot: 'assets', nowrap: true },
  { key: 'roe', title: '加权 ROE %', slot: 'roe', nowrap: true },
  { key: 'eps', title: '基本每股收益', slot: 'eps', nowrap: true },
  { key: 'source', title: '出处', slot: 'source' },
]

const dormant = computed(() => Object.entries(vocabularies.value || {})
  .map(([key, vocabulary]) => ({
    key,
    title: vocabulary.title,
    unused: vocabulary.terms.filter((term) => !term.used).length,
    total: vocabulary.terms.length,
  }))
  .filter((row) => row.unused))

const used = (vocabulary) => vocabulary.terms
  .filter((term) => term.used)
  .slice()
  .sort((a, b) => b.used - a.used)
  .slice(0, 12)
  .map((term) => ({ label: term.label, count: term.used }))

const COMPANY_COLUMNS = [
  { key: 'company_id', title: '公司', slot: 'company' },
  { key: 'sources', title: '配置来源', slot: 'sources', align: 'right', nowrap: true, width: '90px' },
  { key: 'events', title: '事件', align: 'right', nowrap: true, width: '80px' },
  { key: 'evidence_paragraphs', title: '证据段落', slot: 'evidence', align: 'right', nowrap: true, width: '100px' },
  { key: 'examples', title: '来源举例', slot: 'examples' },
]
const TERM_COLUMNS = [
  { key: 'code', title: '码', slot: 'code', width: '180px' },
  // 名称列悬停即给定义；完整的判据在表下的折叠块里。这一页此前只有「码 / 名称 /
  // 使用次数」——一个想弄清「技术水平和业务拓展怎么分」的读者在这里问不出答案，
  // 而定义一直就在处理层里（pipeline.CODE_GUIDE 等）。
  { key: 'label', title: '名称', slot: 'label' },
  { key: 'used', title: '已使用', slot: 'used', align: 'right', nowrap: true, width: '90px' },
]
</script>

<template>
  <PageHeader title="来源与词库"
      note="五个受控词表的取值与判据，以及每个码实际用了多少次。">
    <!-- components/ui/PageHeader.vue 里那个「口径与限制」折叠槽建好之后一直没有页面用过。
         词表页是它最该出现的地方：这些词表的口径与它们量不到的东西，正是读者在这一页
         最容易误解的部分。 -->
    <template #basis>
      两套词表按不同的轴立目：12 类事件类型按<b>风险类型</b>，26 个专题码按<b>行业专题</b>，
      主办方官方数据集按<b>业务动作</b>——因此三者之间是折算关系而不是包含关系，折算表
      <span class="mono">config/event_taxonomy_map.yaml</span> 在计算任何指标之前就已冻结。
      官方金标只覆盖 12 类中的 7 类，其中股权与控制权、高管与人事、ESG 与声誉、诉讼与仲裁、
      回应与澄清五类的金标支持为 0，所以这五类在第三方数据上<b>无从测量</b>，
      而其中第一类恰是本平台语料里最大的一类。使用次数只说这个码被用了多少次，不说它用得对不对。
    </template>
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">

    <section v-if="companies" class="card">
      <h2>
        公司监控范围
        <span class="muted small">
          配置版本 {{ companies.config_version }} · 最近复核 {{ companies.last_reviewed }}
        </span>
      </h2>
      <DataTable :columns="COMPANY_COLUMNS" :rows="companies.companies" row-key="company_id">
        <template #company="{ row }">
          {{ companyLabel(row.company_id) }}
          <span class="muted small mono"> {{ row.company_id }}</span>
        </template>
        <template #sources="{ row }"><span class="mono">{{ row.sources.length }}</span></template>
        <template #evidence="{ row }">
          <span class="mono">{{ row.evidence_paragraphs.toLocaleString() }}</span>
        </template>
        <template #examples="{ row }">
          <span class="muted small">
            {{ row.sources.slice(0, 4).map((source) => source.name || '—').join('、') }}
            <template v-if="row.sources.length > 4">…</template>
          </span>
        </template>
      </DataTable>
    </section>

    <section v-if="financials?.available" class="card">
      <h2>
        公司主要会计数据
        <span class="muted small">
          {{ financials.periods }} 个报告期 · {{ financials.figures }} 个数字 · 取自定期报告
        </span>
      </h2>
      <DataTable :columns="FIN_COLUMNS" :rows="financialRows" row-key="key">
        <template #company="{ row }">{{ companyLabel(row.company_id) }}</template>
        <template #period="{ row }">
          <span class="mono">{{ row.period }}</span>
          <span class="muted small period-kind">{{ PERIOD_TYPE_LABELS[row.period_type] || row.period_type }}</span>
        </template>
        <template #revenue="{ row }"><span class="mono">{{ money(row.metrics.revenue?.value) }}</span></template>
        <template #profit="{ row }">
          <span class="mono">{{ money(row.metrics.net_profit_attributable?.value) }}</span>
        </template>
        <template #cash="{ row }">
          <span class="mono">{{ money(row.metrics.operating_cash_flow?.value) }}</span>
        </template>
        <template #assets="{ row }"><span class="mono">{{ money(row.metrics.total_assets?.value) }}</span></template>
        <template #roe="{ row }"><span class="mono">{{ plain(row.metrics.weighted_roe?.value) }}</span></template>
        <template #eps="{ row }"><span class="mono">{{ plain(row.metrics.basic_eps?.value) }}</span></template>
        <template #source="{ row }"><span class="muted small">{{ row.source_title }}</span></template>
      </DataTable>
    </section>

    <section v-for="(vocabulary, key) in vocabularies" :key="key" class="card">
      <h2>{{ vocabulary.title }} <span class="muted small mono">{{ vocabulary.source }}</span></h2>
      <div class="split">
        <DataTable :columns="TERM_COLUMNS" :rows="vocabulary.terms" row-key="code">
          <template #code="{ row }"><span class="mono small">{{ row.code }}</span></template>
          <template #label="{ row }">
            <span class="glossed" :title="unmarked(row.definition)">{{ row.label }}</span>
            <span v-if="row.group" class="muted small group">{{ row.group }}</span>
          </template>
          <template #used="{ row }">
            <span class="mono" :class="{ muted: !row.used }">{{ row.used.toLocaleString() }}</span>
          </template>
        </DataTable>
        <div>
          <EChart
            v-if="used(vocabulary).length"
            :builder="(tokens) => rankedBars(tokens, { items: used(vocabulary) })"
            :label="`${vocabulary.title}中使用最多的词条`"
            :height="Math.max(160, used(vocabulary).length * 22)"
            :table-columns="['词条', '使用次数']"
            :table-rows="vocabulary.terms.map((t) => [t.label, t.used])"
            empty="该词表中还没有任何词条被使用。" />
        </div>
      </div>
      <!-- 默认收起：另一位读者说页面上解释性文字太多（2026-09-06 因此净删了 2,592 字），
           而这一位说分类的依据太笼统。两条都对，答案是收起来但点得开。 -->
      <details class="guides">
        <summary class="small">判据（{{ vocabulary.terms.length }} 条定义{{
          vocabulary.judging_rules ? ' + 判定规则' : '' }}）</summary>
        <dl class="guide-list">
          <template v-for="term in vocabulary.terms" :key="term.code">
            <dt class="mono small">{{ term.code }}</dt>
            <dd class="small">{{ term.definition || '（该取值尚无定义）' }}</dd>
          </template>
        </dl>
        <pre v-if="vocabulary.judging_rules" class="small rules">{{ vocabulary.judging_rules }}</pre>
      </details>
      <p v-if="vocabulary.undeclared.length" class="muted small undeclared">
        数据中出现但词表未声明：
        <span v-for="([code, count]) in vocabulary.undeclared" :key="code" class="mono">
          {{ code }}（{{ count }}）
        </span>
      </p>
    </section>
  </AsyncSection>
</template>

<style scoped>
.guides { margin-top: 8px; }
.guides > summary { cursor: pointer; color: var(--text-secondary); }
.guide-list { display: grid; grid-template-columns: 180px 1fr; gap: 4px 10px; margin: 8px 0 0; }
.guide-list dt { color: var(--text-secondary); }
.guide-list dd { margin: 0; }
.rules { white-space: pre-wrap; margin: 10px 0 0; color: var(--text-secondary); }
.group { margin-left: 6px; }
.period-kind { margin-left: 5px; }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 10px; }
h2 .small { font-weight: 400; margin-left: 6px; }
.split { display: grid; grid-template-columns: 1.1fr 1fr; gap: 20px; align-items: start; }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } }
.undeclared { margin-top: 10px; }
</style>
