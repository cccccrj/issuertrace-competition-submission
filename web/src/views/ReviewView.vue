<script setup>
/**
 * 标注复核 — the most stateful page here and the least forgiving to change.
 *
 * 此前侧栏管这一页叫「专项核查」，页头叫「复核任务中心」：一页两个名字，而且其中一个还是
 * 另一件事的名字。这里复核的是**模型的判定**（事件分级、主张裁决、披露差异判得对不对），
 * 业务事项的核查在 /investigations。
 *
 * Its keyboard contract reaches into child components through `defineExpose`,
 * scrolls by querying a class name in the document, and deliberately does *not*
 * refetch after a judgement, because a refetch reorders the queue and loses the
 * reviewer's place. None of that is visible from the template, so all of it was
 * pinned in `__tests__/views/review.test.js` before this file was touched.
 *
 * The one behaviour that changed: the guard that stops a keystroke in the note
 * box being read as a judgement checked `tagName` alone. That covers the native
 * fields this page uses today and nothing else — a contenteditable, or any
 * composed control that renders a div, would have fallen straight through to
 * `judge()`. It now also checks `isContentEditable` and walks up for a combobox
 * role, so the guard survives a control being swapped.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import { TASK_KIND_LABELS, TASK_STATUS_LABELS, api } from '../api.js'
import EChart from '../charts/EChart.vue'
import { matrixHeatmap } from '../charts/options/index.js'
import Pager from '../components/Pager.vue'
import ReviewTask from '../components/ReviewTask.vue'
import StatTile from '../components/StatTile.vue'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import TechDetails from '../components/TechDetails.vue'
import Select from '../components/ui/Select.vue'
import { allows, session } from '../session.js'
import { isTyping } from '../hotkey.js'

/** 95% 区间（Wilson）。判 2 条得到的 80% 和判 300 条得到的 80%
 *  在页面上曾经完全一样，而它们是两句不同的话。 */
function interval(band) {
  if (!band) return ''
  return `95% 区间 ${(band.low * 100).toFixed(1)}%–${(band.high * 100).toFixed(1)}%，`
}

const filters = ref({ kind: 'verdict', status: 'open', company: '', assignee: '' })
const offset = ref(0)
const limit = 20
const data = ref(null)
const metrics = ref(null)
const loading = ref(false)
const notice = ref(null)
const error = ref(null)
const loadError = ref('')
const SEED_KINDS = [
  { value: 'verdict', label: '主张裁决' },
  { value: 'event', label: '事件抽取' },
  { value: 'diff', label: '披露差异' },
]
const SEED_ORIGINS = [
  { value: 'evaluation_sample', label: '人工评测抽样（按裁决分层）' },
  { value: 'needs_review', label: '处理层标记需复核' },
  { value: 'unverified_major_rumour', label: '高影响低可信预警' },
]
const seeding = ref({ kind: 'verdict', origin: 'evaluation_sample', count: 40 })
const reviewers = ref([])
const dealing = ref({ assignees: [], overlap: 0 })
const cards = ref([])
const focus = ref(0)

/** 默认落在「我的那一份」——但只在确实分派过东西给这个人的时候。
 *
 *  队列的排序是确定的，所以三个人同时打开看到的是同一批前二十五条，然后判的也
 *  是同一批，尾巴没人碰。把默认换成自己那份，撞车就不会发生。但在还没分派过的
 *  库上「我的」是空的，那会让页面看起来像坏了——所以先问一次有没有，没有就照旧
 *  显示全部。 */
async function preferOwnSlice() {
  if (!session.username || !allows('analyst')) return
  try {
    const mine = await api.reviewTasks({ ...filters.value, assignee: 'me', limit: 1 })
    if (mine.total > 0) filters.value.assignee = 'me'
  } catch {
    // 分派尚未可用时照旧显示全部，而不是把整页拦下来。
  }
}

async function loadReviewers() {
  try {
    reviewers.value = (await api.reviewReviewers()).items
  } catch {
    reviewers.value = []
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await api.reviewTasks({ ...filters.value, limit, offset: offset.value })
    focus.value = 0
    await refreshMetrics()
  } catch (exception) {
    loadError.value = exception.message
  } finally {
    loading.value = false
  }
}

const refreshMetrics = async () => { metrics.value = await api.reviewMetrics() }

async function act(promise, message) {
  error.value = null
  notice.value = null
  try {
    await promise
    notice.value = message
    return true
  } catch (exception) {
    error.value = exception.message
    return false
  }
}

/** Judging must not reload the list.  The queue is ordered by status, so a
 *  refetch moves the task just judged to the end and shifts every card below it
 *  upward — three judgements in and the reviewer has lost their place.  The row
 *  is updated where it sits and only the metrics are re-read. */
async function judge(task, body) {
  const ok = await act(api.reviewJudge(task.task_id, body), '已记录判定')
  if (!ok) return
  task.status = 'judged'
  task.decisions = [...task.decisions, {
    decision_id: `local-${Date.now()}`, decided_at: new Date().toISOString(),
    reviewer: session.username, role: session.role, action: 'judge',
    judgement: body.judgement, note: body.note || null,
    correct_label: body.correct_label || null,
    evidence_ok: body.evidence_ok === undefined ? null : body.evidence_ok,
  }]
  await refreshMetrics()
  if (focus.value < (data.value?.items.length || 0) - 1) focus.value += 1
}

async function approve(task) {
  if (await act(api.reviewApprove(task.task_id), '已通过')) task.status = 'approved'
}

async function reject(task, note) {
  if (await act(api.reviewReject(task.task_id, { note }), '已退回')) {
    task.status = 'rejected'
  }
}

const seed = () => act(api.reviewSeed(seeding.value), '已生成复核任务').then(load)

/** 分派的范围就是当前筛选，外加「只发还没判的」。
 *
 *  沿用筛选而不是另开一组控件：先把队列筛成要分的那一批、再按下分派，比在两处
 *  分别描述同一个范围要少一次对不上的机会。 */
async function deal() {
  error.value = null
  notice.value = null
  try {
    const result = await api.reviewAssign({
      assignees: dealing.value.assignees,
      overlap: dealing.value.overlap || 0,
      status: 'open',
      kind: filters.value.kind || null,
      company: filters.value.company || null,
    })
    const split = Object.entries(result.assigned)
      .map(([person, count]) => `${person} ${count}`).join(' / ')
    notice.value = `已分派 ${result.tasks} 条：${split}`
      + (result.overlap ? `，其中 ${result.overlap} 条双标（每人都判）` : '')
    await load()
  } catch (exception) {
    error.value = exception.message
  }
}

/** The queue's default is 待复核主张裁决, which is the work — not a neutral
 *  view of the table. Resetting therefore returns to that default rather than
 *  clearing to everything, and the count reflects departures from it. */
const DEFAULTS = { kind: 'verdict', status: 'open', company: '', assignee: '' }
const activeCount = computed(() =>
  Object.entries(DEFAULTS).filter(([key, value]) => filters.value[key] !== value).length)
const reset = () => { filters.value = { ...DEFAULTS } }

/** The confusion matrix has been computed and returned by /api/review/metrics
 *  from the beginning, and the page read only the accuracy and the macro-F1 out
 *  of it. A macro-F1 says how well the classifier does overall; the matrix says
 *  *which* class it mistakes for which, which is the thing a reviewer can act
 *  on. */
const matrices = computed(() => Object.entries(metrics.value?.by_kind || {})
  .map(([kind, row]) => {
    const confusion = row.confusion || {}
    const said = Object.keys(confusion)
    const truth = [...new Set(said.flatMap((key) => Object.keys(confusion[key] || {})))]
    if (!said.length || !truth.length) return null
    const cells = []
    said.forEach((row_key, y) => truth.forEach((column, x) => {
      cells.push([x, y, confusion[row_key]?.[column] || 0])
    }))
    return { kind, said, truth, cells, per_class: row.per_class || {} }
  })
  .filter(Boolean))

const PER_CLASS_COLUMNS = [
  { key: 'label', title: '类别' },
  { key: 'precision', title: '精确率', slot: 'precision', align: 'right', width: '74px' },
  { key: 'recall', title: '召回', slot: 'recall', align: 'right', width: '68px' },
  { key: 'f1', title: 'F1', slot: 'f1', align: 'right', width: '62px' },
  { key: 'support', title: '支持', align: 'right', width: '62px' },
]

const perClassRows = (matrix) => Object.entries(matrix.per_class)
  .map(([label, scores]) => ({ label, ...scores }))

const REVIEWER_COLUMNS = [
  { key: 'reviewer', title: '账号' },
  { key: 'kind_label', title: '类型' },
  { key: 'judged', title: '已判', align: 'right', width: '58px' },
  { key: 'correct', title: '正确', align: 'right', width: '58px' },
  { key: 'wrong', title: '错误', align: 'right', width: '58px' },
  { key: 'unsure', title: '无法判断', align: 'right', width: '76px' },
  { key: 'system_accuracy', title: '系统准确率', slot: 'rate', align: 'right', width: '104px' },
]

const reviewerRows = computed(() => (metrics.value?.by_reviewer || []).map((row) => ({
  ...row,
  key: `${row.reviewer}-${row.kind}`,
  kind_label: TASK_KIND_LABELS[row.kind] || row.kind,
  correct: row.correct || 0,
  wrong: row.wrong || 0,
  unsure: row.unsure || 0,
})))

const agreementRows = computed(() => Object.entries(metrics.value?.agreement || {})
  .flatMap(([kind, pairs]) => pairs.map((row) =>
    ({ ...row, kind, key: `${kind}-${row.pair.join('-')}` }))))

const assigneeOptions = computed(() => [
  { value: 'me', label: '我的' },
  ...reviewers.value
    .filter((person) => person.username !== session.username)
    .map((person) => ({ value: person.username, label: person.username })),
])

const judgedHere = computed(() =>
  (data.value?.items || []).filter((task) => task.status !== 'open').length)

// 1 / 2 / 3 judge the focused card, j / k move between them.  Typing in the note
// box must never be read as a judgement, so fields are excluded.
function onKey(event) {
  if (!allows('analyst') || event.metaKey || event.ctrlKey || event.altKey) return
  if (isTyping(event.target)) return
  const items = data.value?.items || []
  if (!items.length) return
  const keys = { 1: 'correct', 2: 'wrong', 3: 'unsure' }
  if (keys[event.key]) {
    cards.value[focus.value]?.judge(keys[event.key])
  } else if (event.key === 'j' || event.key === 'ArrowDown') {
    focus.value = Math.min(focus.value + 1, items.length - 1)
  } else if (event.key === 'k' || event.key === 'ArrowUp') {
    focus.value = Math.max(focus.value - 1, 0)
  } else {
    return
  }
  event.preventDefault()
  document.querySelectorAll('.task')[focus.value]?.scrollIntoView({ block: 'nearest' })
}

watch(filters, () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
loadReviewers()
// 先决定看谁的那一份，再取列表：反过来会先闪一遍全部队列。`preferOwnSlice` 只在
// 真的改了筛选时才触发那次 watch，所以常见情形仍然只有一次请求。
preferOwnSlice().then(() => { if (!filters.value.assignee) load() })
</script>

<template>
  <PageHeader title="标注复核"
      note="系统研判结果的人工复核队列；判定不改写系统已有的结论。">
  </PageHeader>

  <div v-if="metrics" class="tiles">
    <StatTile label="任务总数" :value="metrics.tasks" />
    <StatTile label="待复核" :value="metrics.queue.open || 0" />
    <template v-for="(row, kind) in metrics.by_kind" :key="kind">
      <!-- 区间和数字一起给：「准确率 80%」判 2 条和判 300 条，此前在页面上
           长得一模一样。区间同时也是「判多少条才够」的停止规则。 -->
      <StatTile :label="`${TASK_KIND_LABELS[kind] || kind}准确率`"
                :value="row.accuracy === null ? '—' : `${(row.accuracy * 100).toFixed(1)}%`"
                :note="`${interval(row.accuracy_interval)}样本量 ${row.sample_size}，另有 ${row.unsure || 0} 条无法判断`" />
      <StatTile :label="`${TASK_KIND_LABELS[kind] || kind} macro-F1`"
                :value="row.macro_f1 === null ? '—' : row.macro_f1.toFixed(3)"
                :note="`${row.labelled} 条进入混淆矩阵${row.wrong_without_label
                  ? `，${row.wrong_without_label} 条判错未给正确答案` : ''}`" />
      <StatTile v-if="row.evidence_location" label="证据定位准确率"
                :value="`${(row.evidence_location.accuracy * 100).toFixed(1)}%`"
                :note="`已判 ${row.evidence_location.judged} 条引用`" />
    </template>
    <!-- 共用一个账号时这个数永远是 0：分歧被写成了同一个人改主意。 -->
    <StatTile v-if="metrics.double_judged" label="双标任务" :value="metrics.double_judged"
              :note="`两个人判过同一条，其中 ${metrics.contested} 条判得不同`" />
  </div>
  <!-- Before anything has been judged the page says so, rather than showing a
       rate computed from nothing. -->
  <p v-if="metrics && !Object.keys(metrics.by_kind).length" class="page-note">
    暂无人工判定记录，准确率一栏为空，表示尚未测量，而非 0%。
  </p>

  <section v-if="reviewerRows.length" class="card">
    <h2>按账号统计</h2>
    <p class="muted small">
      此栏为<b>各复核人判定得出的系统准确率</b>，并非复核人本身的准确率（无金标，无法评价复核人）。
      并列展示用于发现判据分歧：若同一批任务上各人的比率差异较大，说明对判据的理解不一致，
      应先统一判据再继续复核。
    </p>
    <DataTable :columns="REVIEWER_COLUMNS" :rows="reviewerRows" row-key="key">
      <template #rate="{ row }">
        <span class="mono">{{ row.system_accuracy === null
          ? '—' : `${(row.system_accuracy * 100).toFixed(1)}%` }}</span>
      </template>
    </DataTable>
    <div class="agreement">
      <b>标注一致性</b>
      <ul v-if="agreementRows.length">
        <li v-for="row in agreementRows" :key="row.key">
          {{ row.pair[0] }} × {{ row.pair[1] }}（{{ TASK_KIND_LABELS[row.kind] || row.kind }}）：
          <template v-if="row.kappa === null">
            同判 {{ row.n }} 条，一致率 {{ (row.observed * 100).toFixed(0) }}%。由于两人仅使用过同一种判定，
            κ 无法计算，该结果不代表「完全一致」。
          </template>
          <template v-else>
            κ <b class="mono">{{ row.kappa.toFixed(2) }}</b>，
            一致率 {{ (row.observed * 100).toFixed(0) }}%，同判 {{ row.n }} 条。
            κ 已剔除由各自判定分布导致的偶然一致，因此两人若对所有任务均判「正确」，κ 为 0。
          </template>
        </li>
      </ul>
      <p v-else class="muted small">
        暂无两人共同判定的任务，无法计算一致性（不代表为 0）。
        分派任务时设置「双标条数」即可产生可计算样本。
      </p>
    </div>
  </section>

  <section v-for="matrix in matrices" :key="matrix.kind" class="card">
    <h2>{{ TASK_KIND_LABELS[matrix.kind] || matrix.kind }}：混淆矩阵</h2>
    <div class="matrix">
      <EChart
        :builder="(tokens) => matrixHeatmap(tokens, { rows: matrix.said, columns: matrix.truth, cells: matrix.cells })"
        label="系统所答与复核者认为应答的混淆矩阵，每格标出计数"
        :height="220"
        :table-columns="['系统所答', '应答', '计数']"
        :table-rows="matrix.cells.map((cell) => [matrix.said[cell[1]], matrix.truth[cell[0]], cell[2]])" />
      <div v-if="Object.keys(matrix.per_class).length" class="per-class">
        <DataTable :columns="PER_CLASS_COLUMNS" :rows="perClassRows(matrix)" row-key="label">
          <template #precision="{ row }"><span class="mono">{{ row.precision?.toFixed?.(2) ?? '—' }}</span></template>
          <template #recall="{ row }"><span class="mono">{{ row.recall?.toFixed?.(2) ?? '—' }}</span></template>
          <template #f1="{ row }"><span class="mono">{{ row.f1?.toFixed?.(2) ?? '—' }}</span></template>
        </DataTable>
      </div>
    </div>
  </section>

  <Callout v-if="notice" tone="ok">{{ notice }}</Callout>
  <Callout v-if="error" tone="bad">{{ error }}</Callout>

  <div v-if="allows('reviewer')" class="card seed">
    <b>生成复核任务</b>
    <div class="seed-row">
      <Select v-model="seeding.kind" label="复核类型" :options="SEED_KINDS" />
      <Select v-model="seeding.origin" label="入列理由" :options="SEED_ORIGINS" />
      <input v-model.number="seeding.count" type="number" min="1" max="500"
             aria-label="生成数量" title="生成数量" />
      <button @click="seed">生成</button>
    </div>
    <TechDetails hint="抽样机制">
      抽样是确定性的：同样的参数取到同一批样本，评测才能复跑。已存在的任务不会重复生成。
    </TechDetails>
  </div>

  <div v-if="allows('reviewer')" class="card seed">
    <b>分派复核任务</b>
    <div class="seed-row">
      <div v-if="reviewers.length" class="people">
        <label v-for="person in reviewers" :key="person.username" class="person">
          <input v-model="dealing.assignees" type="checkbox" :value="person.username" />
          {{ person.username }}<span class="muted">（{{ person.role_label }}）</span>
        </label>
      </div>
      <span v-else class="muted small">暂无可执行判定的账号，仅分析师及以上角色会在此显示。</span>
      <label class="overlap">双标
        <input v-model.number="dealing.overlap" type="number" min="0" max="500" />条
      </label>
      <button :disabled="!dealing.assignees.length" @click="deal">按当前筛选分派</button>
    </div>
    <TechDetails hint="切分与双标">
      分的是当前筛选（{{ TASK_KIND_LABELS[filters.kind] || '全部类型'
      }}<template v-if="filters.company"> · {{ filters.company }}</template>）里<b>待复核</b>的那些；
      已判定的任务不会重新分配。切分结果是确定的，同一组人员重新分派将得到相同结果。
      前 {{ dealing.overlap || 0 }} 条同时分派给每位复核人，作为计算一致性（κ）的唯一样本来源。
    </TechDetails>
  </div>

  <FilterBar :active="activeCount" @reset="reset">
    <EnumSelect v-model="filters.kind" :labels="TASK_KIND_LABELS" all-label="全部类型" />
    <EnumSelect v-model="filters.status" :labels="TASK_STATUS_LABELS" all-label="全部状态" />
    <CompanySelect v-model="filters.company" />
    <EnumSelect v-if="allows('analyst')" v-model="filters.assignee"
                :options="assigneeOptions" all-label="全部复核人" />
    <template #end>
      <span v-if="data?.items.length" class="muted small">
        本页已判 {{ judgedHere }} / {{ data.items.length }} ·
        当前筛选共 {{ data.total.toLocaleString() }} 条
        <template v-if="allows('analyst')">· 快捷键 <b class="mono">1</b> 正确
          <b class="mono">2</b> 错误 <b class="mono">3</b> 无法判断 <b class="mono">j/k</b> 上下</template>
      </span>
    </template>
  </FilterBar>

  <AsyncSection
    :loading="loading" :error="loadError" :empty="!data?.items.length"
    :empty-text="`没有符合条件的复核任务${allows('reviewer') ? '，可在上方生成一批。' : '。'}`"
  >
    <ReviewTask v-for="(task, index) in data.items" :key="task.task_id" ref="cards"
                :task="task" :focused="index === focus" :writable="allows('analyst')"
                :moderator="allows('reviewer')"
                @judge="judge" @approve="approve" @reject="reject" />
    <Pager :total="data.total" :limit="limit" :offset="offset" @update:offset="offset = $event" />
  </AsyncSection>
</template>

<style scoped>
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr)); gap: 12px; margin-bottom: 12px; }
.seed { margin-bottom: 12px; }
.seed-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0 6px; }
.seed-row input { width: 84px; font: inherit; padding: 6px 9px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 6px; }
.matrix { display: grid; grid-template-columns: 1.2fr 1fr; gap: 20px; align-items: start; }
@media (max-width: 1100px) { .matrix { grid-template-columns: 1fr; } }
.per-class { font-size: var(--fs-aux); }
.people { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
.person { display: inline-flex; gap: 5px; align-items: center; font-size: var(--fs-aux); }
.overlap { display: inline-flex; gap: 5px; align-items: center; font-size: var(--fs-aux);
  color: var(--text-secondary); }
.overlap input { width: 64px; }
.agreement { margin-top: 12px; font-size: var(--fs-aux); }
/* κ 是这一段的结论。per-class 那张表不动：表格单元格放大会破坏行密度，
   而那是一张用来横向比较的表，不是一个要被先看到的数。 */
.agreement li b { font-size: var(--fs-result); color: var(--text-primary); letter-spacing: -0.01em; }
.agreement ul { margin: 6px 0 0; padding-left: 18px; }
.agreement li { margin-bottom: 4px; color: var(--text-secondary); }
</style>
