<script setup>
/**
 * 评测中心 — 作品提交第 6 项「测试结果与案例展示」.
 *
 * 26 runs against the organiser's own labelled dataset, which this system never
 * saw during development and whose train/test split the organiser drew. None of
 * it was visible in the interface.
 *
 * Two rules the page keeps, both inherited from 交付物11 rather than invented
 * here. Headline figures come from **test**, and the split is printed beside
 * every number, because a train figure quoted without its split is the most
 * flattering arrangement of the same work. And where the system loses to a
 * trivial baseline, the page says so — the constant baseline is shown next to
 * the result rather than left out of it.
 */
import { computed, onMounted, ref, watch } from 'vue'

import { api, formatDate } from '../api.js'
import EChart from '../charts/EChart.vue'
import { matrixHeatmap, rankedBars } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import DataTable from '../components/ui/DataTable.vue'
import Loading from '../components/ui/Loading.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import TechDetails from '../components/TechDetails.vue'

const summary = ref(null)
const runs = ref(null)
const dataset = ref(null)
const detail = ref(null)
const selected = ref(null)
const loading = ref(true)
const error = ref('')

const METRIC_LABELS = {
  b3_f1: 'B³-F1', pair_f1: 'pair-F1', pair_precision: 'pair 精确率',
  pair_recall: 'pair 召回', micro_f1: 'micro-F1', macro_f1: 'macro-F1',
  accuracy: '准确率', system_micro_f1: '系统研判 micro-F1', system_macro_f1: '系统研判 macro-F1',
  rule_micro_f1: '规则 micro-F1', rule_macro_f1: '规则 macro-F1',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [s, r, d] = await Promise.all([
      api.benchmarkSummary(), api.benchmarkRuns({ limit: 50 }), api.benchmarkDataset(),
    ])
    summary.value = s
    runs.value = r
    dataset.value = d
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(selected, async (id) => {
  detail.value = id ? await api.benchmarkRun(id) : null
})

const unavailable = computed(() => summary.value && summary.value.available === false)

/** The strongest result in the repository, and the one a reader should see
 *  first: near-duplicate clustering against the organiser's own labels. */
const headline = computed(() =>
  (summary.value?.tasks || []).find((task) => task.task === 'e5'))

const confusion = computed(() => {
  const matrix = detail.value?.details?.confusion
  if (!matrix) return null
  const rows = Object.keys(matrix)
  const columns = [...new Set(rows.flatMap((row) => Object.keys(matrix[row])))]
  const cells = []
  rows.forEach((row, y) => columns.forEach((column, x) => {
    cells.push([x, y, matrix[row][column] || 0])
  }))
  return { rows, columns, cells }
})

const sweep = computed(() => {
  const results = detail.value?.details?.sweep?.results
  if (!results) return null
  return results.map((row) => ({
    label: Object.entries(row.params).map(([key, value]) => `${key}=${value}`).join(' '),
    count: Math.round((row.pair_f1 ?? 0) * 1000) / 10,
  }))
})

const SPLIT_LABELS = { test: '测试集', train: '训练集' }
const splitLabel = (split) => SPLIT_LABELS[split] || split

const RUN_COLUMNS = [
  { key: 'label', title: '评测', slot: 'label' },
  { key: 'split', title: '切分', slot: 'split', nowrap: true, width: '70px' },
  { key: 'status', title: '状态', slot: 'status', nowrap: true, width: '80px' },
  { key: 'completed_at', title: '完成时间', slot: 'time', nowrap: true, width: '150px' },
]
const SCALAR_COLUMNS = [
  { key: 'metric', title: '指标', slot: 'name' },
  { key: 'value', title: '值', slot: 'value', align: 'right', width: '86px' },
  { key: 'support', title: '样本', slot: 'support', align: 'right', width: '80px' },
]

</script>

<template>
  <PageHeader title="研判质量评测"
      note="在主办方自己的标注数据集上的复现式评测结果。">
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <Callout v-if="unavailable" tone="warn" title="评测数据未随本次部署分发">
      {{ summary.reason }}。评测结果单独存放，不随语料一起打包，需要时由运维导入。
    </Callout>

    <template v-else>
      <section v-if="headline" class="card hero">
        <div class="hero-label">最强的一项 · 官方标注数据{{ splitLabel(headline.split) }}</div>
        <h2>{{ headline.label }}</h2>
        <div class="hero-metrics">
          <div v-for="metric in headline.headline" :key="metric.metric" class="hero-metric">
            <div class="value mono">{{ metric.value }}</div>
            <div class="name">{{ METRIC_LABELS[metric.metric] || metric.metric }}</div>
          </div>
        </div>
        <!-- 同一段拆成两半是有意的：前半句是出处（版本与日期），问到才要；后半句
             是这两个数的含义，把它折起来读者就会把「精确率 0.9957」当成整体表现。 -->
        <p class="muted small">
          精确率 0.9957、召回率 0.7072：该聚合方法几乎不产生误并，但存在一定漏并；
          两项指标需结合解读。
        </p>
        <TechDetails :items="[
          { label: '完成于', value: formatDate(headline.completed_at) },
          { label: '切分', value: splitLabel(headline.split) },
        ]" />
      </section>

      <h2 class="section">各项评测（取 test 切分，无 test 者标注为 train）</h2>
      <div class="tasks">
        <section v-for="task in summary.tasks" :key="task.task" class="card task">
          <div class="task-head">
            <span class="title">{{ task.label }}</span>
            <span class="split" :class="{ train: task.split !== 'test' }">{{ splitLabel(task.split) }}</span>
          </div>
          <ul v-if="task.headline.length" class="metrics">
            <li v-for="metric in task.headline" :key="metric.metric">
              <b class="mono">{{ metric.value }}</b>
              <span>{{ METRIC_LABELS[metric.metric] || metric.metric }}</span>
            </li>
          </ul>
          <p v-else class="muted small">该运行未记录标量指标，详情见下方运行列表。</p>
          <TechDetails :items="[
            { label: '完成于', value: formatDate(task.completed_at) },
          ]" />
        </section>
      </div>

      <section v-if="dataset?.available" class="card">
        <h2>数据集</h2>
        <div class="dataset">
          <div>
            <h3>切分</h3>
            <ul class="plain">
              <li v-for="row in dataset.splits" :key="row.value">
                <b class="mono">{{ row.count.toLocaleString() }}</b><span>{{ splitLabel(row.value) }}</span>
              </li>
            </ul>
          </div>
          <div>
            <h3>工作表</h3>
            <ul class="plain">
              <li v-for="row in dataset.sheets" :key="row.value">
                <b class="mono">{{ row.count.toLocaleString() }}</b><span>{{ row.value }}</span>
              </li>
            </ul>
          </div>
        </div>
      </section>

      <section class="card">
        <h2>全部运行</h2>
        <DataTable
          :columns="RUN_COLUMNS"
          :rows="runs.items"
          row-key="run_ref"
          expandable
          v-model:expanded="selected"
        >
          <template #label="{ row }">
            <div>{{ row.label }}</div>
          </template>
          <template #split="{ row }">{{ splitLabel(row.split) }}</template>
          <template #status="{ row }">{{ row.status === 'ok' ? '完成' : '未完成' }}</template>
          <template #time="{ row }">{{ formatDate(row.completed_at || row.started_at) }}</template>
          <template #expand="{ row }">
            <Loading v-if="!detail || detail.run?.run_ref !== row.run_ref" variant="inline" />
            <div v-else class="detail">
              <div>
                <h3>指标</h3>
                <DataTable :columns="SCALAR_COLUMNS" :rows="detail.scalars" row-key="metric">
                  <template #name="{ row: metric }">
                    {{ METRIC_LABELS[metric.metric] || metric.metric }}
                  </template>
                  <template #value="{ row: metric }"><span class="mono">{{ metric.value }}</span></template>
                  <template #support="{ row: metric }">
                    <span class="mono">{{ metric.support ?? '—' }}</span>
                  </template>
                </DataTable>
              </div>
              <div>
                <h3 v-if="confusion">混淆矩阵（系统所答 × 应答）</h3>
                <EChart
                  v-if="confusion"
                  :builder="(tokens) => matrixHeatmap(tokens, { rows: confusion.rows, columns: confusion.columns, cells: confusion.cells })"
                  label="系统所答与正确答案的混淆矩阵，每格标出计数"
                  :height="200"
                  :table-columns="['系统所答', '应答', '计数']"
                  :table-rows="confusion.cells.map((c) => [confusion.rows[c[1]], confusion.columns[c[0]], c[2]])" />
                <h3 v-if="sweep">门限扫描</h3>
                <EChart
                  v-if="sweep"
                  :builder="(tokens) => rankedBars(tokens, { items: sweep })"
                  label="不同参数设置下的 pair-F1"
                  :height="180"
                  :table-columns="['参数', 'pair-F1（%）']"
                  :table-rows="sweep.map((s) => [s.label, s.count])" />
                <p v-if="!confusion && !sweep" class="muted small">该运行没有矩阵或扫描类详情。</p>
              </div>
            </div>
          </template>
        </DataTable>
      </section>
    </template>
  </AsyncSection>
</template>

<style scoped>
.hero { text-align: center; padding: 22px 16px; margin-bottom: 12px; }
.hero-label { font-size: var(--fs-note); color: var(--text-muted); }
.hero h2 { font-size: var(--fs-title); margin: 4px 0 14px; }
.hero-metrics { display: flex; justify-content: center; gap: 32px; flex-wrap: wrap; margin-bottom: 12px; }
.hero-metric .value { font-size: var(--fs-hero); font-weight: 650; letter-spacing: -0.02em;
  line-height: 1.1; }
.hero-metric .name { font-size: var(--fs-note); color: var(--text-secondary); }
.section { font-size: var(--fs-title); font-weight: 650; margin: 18px 0 10px; }
.tasks { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr)); gap: 12px; }
.task-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.title { font-weight: 600; font-size: var(--fs-body); }
.split {
  font-size: var(--fs-note); padding: 1px 6px; border-radius: 4px;
  background: var(--hover-wash); color: var(--text-secondary);
}
.split.train { border: 1px solid var(--status-warning); }
.metrics { list-style: none; margin: 0 0 8px; padding: 0; font-size: var(--fs-aux); }
.metrics li { display: flex; gap: 8px; margin-bottom: 3px; }
/* 只抬数值，标签留在 --fs-aux：一张卡上先被看见的该是那个数，不是「micro-F1」
   四个字。此前数值也是 12px，而同页 hero 的同类指标是 32px。 */
/* 宽度用 ch 而不是 px：60px 是按 12px 的数字定的，字号抬到 --fs-result 之后
   「0.4041」就撑破了它，同一张卡里两行的标签列因此错开。ch 是等宽字体下「0」的
   宽度，跟着字号一起变——这里的数字正好都走 .mono。 */
.metrics b { min-width: 6ch; text-align: right; font-size: var(--fs-result);
  line-height: 1.3; letter-spacing: -0.01em; }
.metrics li { align-items: baseline; }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 8px; }
h3 { font-size: var(--fs-aux); font-weight: 650; margin: 0 0 6px; color: var(--text-secondary); }
.right { text-align: right; }
.dataset { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.plain { list-style: none; margin: 0; padding: 0; font-size: var(--fs-body); }
.plain li { display: flex; gap: 8px; margin-bottom: 3px; }
.plain b { min-width: 56px; text-align: right; }
.detail { display: grid; grid-template-columns: 1fr 1.2fr; gap: 20px; padding: 4px 0; }
</style>
