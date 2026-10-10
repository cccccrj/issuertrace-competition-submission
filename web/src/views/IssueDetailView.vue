<script setup>
/**
 * 议题详情 — one issue's claim matrix, its facts, and how its status got there.
 *
 * The status word (新出现 / 升温 / 活跃 / 降温 / 沉寂) is derived and the ledger
 * keeps only the current one. `issue_status_history` has recorded every change
 * since the timeline stage was written; nothing read it, so a reader could see
 * 「降温」 without being able to see it had been 「升温」 the week before —
 * which is the half that says whether to act. The trail is now on the page,
 * with the counts on both sides of each change and the reason the stage gave.
 */
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  api, basisLabel, companyLabel, formatDate, targetText, ISSUE_LABELS, ISSUE_STATUS_LABELS, issueLabel, LEVEL_META, levelLabel,
} from '../api.js'
import { allows } from '../session.js'
import LevelBadge from '../components/LevelBadge.vue'
import VerdictBar from '../components/VerdictBar.vue'
import EChart from '../charts/EChart.vue'
import { stackedTrend } from '../charts/options/index.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import Select from '../components/ui/Select.vue'

const route = useRoute()
const issue = ref(null)
const error = ref('')
const adjustments = ref([])

async function load(id) {
  issue.value = null
  error.value = ''
  try {
    const [detail, log] = await Promise.all([api.issue(id), api.issueAdjustments()])
    issue.value = detail
    adjustments.value = log.items
  } catch (exception) {
    error.value = exception.message
  }
}

watch(() => route.params.id, load, { immediate: true })

/** 热度趋势：时间线本来就带着每个事件的时间，按月数一下、按风险等级叠起来。不新增接口——
 *  方案 §6.2 要的「热度趋势」是已有数据换一种呈现。中间没有事件的月份补 0，时间轴才是连续的。 */
const heatMonths = computed(() => {
  const months = (issue.value?.timeline || []).map((e) => (e.sort_time || '').slice(0, 7)).filter(Boolean).sort()
  if (!months.length) return []
  const out = []
  let [y, m] = months[0].split('-').map(Number)
  const [ly, lm] = months[months.length - 1].split('-').map(Number)
  while (y < ly || (y === ly && m <= lm)) {
    out.push(`${y}-${String(m).padStart(2, '0')}`)
    m += 1
    if (m > 12) { m = 1; y += 1 }
  }
  return out
})
const heatSeries = computed(() => ['high', 'medium', 'low'].map((key) => {
  const counts = {}
  for (const e of issue.value?.timeline || []) {
    if ((e.risk_level || '').toLowerCase() === key) {
      const month = (e.sort_time || '').slice(0, 7)
      counts[month] = (counts[month] || 0) + 1
    }
  }
  return { key, label: levelLabel(key), colour: LEVEL_META[key]?.color,
           points: heatMonths.value.map((month) => counts[month] || 0) }
}))
const heatRows = computed(() => heatMonths.value.map((month, index) => [
  month, ...heatSeries.value.map((series) => series.points[index]),
]))

/** M4-6 —「归并错了要能救」。
 *
 *  一个议题就是 (公司, 议题码)，事件落进来是因为标注给它打了那个码；打错了，
 *  台账就是错的，而此前没有任何出口。改派是唯一的原语：把事件移出当前议题码，
 *  移进另一个，或者移出台账。**全选是合并，选一部分是拆分**，同一件事的两种用法。
 *
 *  目标只能选受控词表里已有的码。为了装下一次拆分而临时造一个议题名，等于往
 *  页面上放一个 `config/sources.yaml` 从没听说过的值——而主数据与词表那一页
 *  存在的意义，正是把这种值判为缺陷而不是新词条。 */
const canAdjust = computed(() => allows('reviewer'))
const adjusting = ref(false)
const busy = ref(false)
const notice = ref(null)
const draft = ref({ to_code: '', reason: '', event_ids: [] })

const targetOptions = computed(() => [
  { value: '', label: '移出台账（不属于任何专题）' },
  ...Object.entries(ISSUE_LABELS)
    .filter(([code]) => code !== issue.value?.issue_code)
    .sort((a, b) => a[1].localeCompare(b[1], 'zh'))
    .map(([value, label]) => ({ value, label })),
])

const mine = computed(() => adjustments.value.filter(
  (row) => !row.undone_at
    && (row.from_code === issue.value?.issue_code || row.to_code === issue.value?.issue_code)))

function toggleEvent(eventId) {
  const chosen = draft.value.event_ids
  draft.value = {
    ...draft.value,
    event_ids: chosen.includes(eventId)
      ? chosen.filter((id) => id !== eventId)
      : [...chosen, eventId],
  }
}

async function submit() {
  busy.value = true
  notice.value = null
  try {
    const result = await api.issueReassign({
      company_id: issue.value.company_id,
      from_code: issue.value.issue_code,
      // 空串表示「移出台账」，与「移到某个码」是两种不同的决定。
      to_code: draft.value.to_code || null,
      event_ids: draft.value.event_ids.length ? draft.value.event_ids : null,
      reason: draft.value.reason,
    })
    notice.value = { tone: 'ok', text: `已改派 ${result.moved} 条事件` }
    adjusting.value = false
    draft.value = { to_code: '', reason: '', event_ids: [] }
    await load(route.params.id)
  } catch (exception) {
    notice.value = { tone: 'bad', text: exception.message }
  } finally {
    busy.value = false
  }
}

async function undo(adjustmentId) {
  busy.value = true
  try {
    await api.issueUndoAdjustment(adjustmentId)
    await load(route.params.id)
  } catch (exception) {
    notice.value = { tone: 'bad', text: exception.message }
  } finally {
    busy.value = false
  }
}

const adjudicated = computed(() =>
  Object.values(issue.value?.verdicts || {}).reduce((sum, count) => sum + count, 0))

const STATUS_LABELS = ISSUE_STATUS_LABELS

/** The arrow reads the event counts, not the status words.
 *
 *  It first ranked the five words on an invented scale, and the corpus
 *  immediately produced 「沉寂 → 降温」 with the count going 1 → 0: an upward
 *  arrow over a fall. The words are a state machine the processor owns and
 *  their ordering is not mine to declare; the counts on either side of the
 *  change are measured, so the arrow reports those and the words speak for
 *  themselves. */
const arrow = (row) => {
  const delta = (row.recent_event_count ?? 0) - (row.previous_event_count ?? 0)
  return delta > 0 ? '↑' : delta < 0 ? '↓' : '→'
}
</script>

<template>
  <AsyncSection
    :loading="!issue && !error" :error="error" :empty="false"
    empty-text="未找到该专题，链接可能来自其他批次的采集。"
  >
    <PageHeader
      :title="issueLabel(issue.issue_code)"
      note="这个专题的全量事件、裁决构成与状态变化。"
      :crumbs="[{ label: '专题跟踪', to: '/issues' },
                { label: companyLabel(issue.company_id), to: `/issues?company=${issue.company_id}` }]"
    >
      <template #actions>
        <RouterLink :to="`/report/issues/${issue.issue_id}`">导出报告 →</RouterLink>
      </template>
    </PageHeader>

    <div class="grid">
      <section class="card">
        <h2>主张矩阵</h2>
        <VerdictBar v-if="adjudicated" :counts="issue.verdicts" :total="adjudicated" />
        <p v-else class="muted small">该专题下还没有已裁决的主张。</p>
      </section>

      <!-- 案例区：这一页在讲的那个议题本身。 -->
      <section class="card subject">
        <h2>专题概况</h2>
        <dl class="detail-grid">
          <dt>风险分</dt>
          <dd><LevelBadge :level="(issue.risk_level || '').toLowerCase()" :score="issue.risk_score" /></dd>
          <dt>首次出现</dt>
          <dd class="mono">{{ formatDate(issue.first_seen_at) }}</dd>
          <dt>最近出现</dt>
          <dd class="mono">{{ formatDate(issue.last_seen_at) }}</dd>
          <!-- Event-time span is reported separately from the publication span,
               because the two answer different questions. -->
          <dt>事件时间跨度</dt>
          <dd class="mono">
            {{ formatDate(issue.event_time_earliest) }} → {{ formatDate(issue.event_time_latest) }}
          </dd>
          <!-- 列表页一直有这三个数，详情页此前没有——方案 §6.2 的「独立信源数」就在这里。
               「独立信源」与「事件数」「文档数」分开说：十篇转载是一个声音，不是十个。 -->
          <dt>事件 / 文档</dt>
          <dd class="mono">{{ issue.event_count ?? '—' }} / {{ issue.document_count ?? '—' }}</dd>
          <dt>独立信源</dt>
          <dd class="mono">{{ issue.independent_source_count ?? '—' }}</dd>
          <dt>最近 30 天</dt>
          <dd class="mono">{{ issue.recent_event_count ?? '—' }} 起事件</dd>
        </dl>
      </section>
    </div>

    <section class="card block">
      <h2>热度趋势</h2>
      <EChart
        v-if="heatMonths.length"
        :builder="(tokens) => stackedTrend(tokens, { periods: heatMonths, series: heatSeries })"
        label="这个专题的事件按月份计数，按风险等级堆叠"
        :height="160"
        :table-columns="['月份', ...heatSeries.map((series) => series.label)]"
        :table-rows="heatRows" />
      <p v-else class="muted small">该专题下暂无带时间信息的事件。</p>
    </section>

    <section class="card block">
      <h2>状态变化</h2>
      <p v-if="!issue.status_history?.length" class="muted small">
        该专题状态自创建以来未发生变化，始终为「{{ STATUS_LABELS[issue.status] || issue.status }}」。
      </p>
      <ol v-else class="trail">
        <li v-for="(row, index) in issue.status_history" :key="index">
          <span class="mono when">{{ formatDate(row.changed_at) }}</span>
          <span class="move">
            <span class="muted">{{ STATUS_LABELS[row.previous_status] || row.previous_status || '首次判定' }}</span>
            <span class="arrow" aria-hidden="true">{{ arrow(row) }}</span>
            <b>{{ STATUS_LABELS[row.status] || row.status }}</b>
          </span>
          <span class="muted small why">
            {{ row.reason }}（{{ row.previous_event_count }} → {{ row.recent_event_count }} 个事件）
          </span>
        </li>
      </ol>
    </section>

    <section v-if="mine.length" class="card block">
      <h2>人工改派（{{ mine.length }} 条生效中）</h2>
      <ul class="adjustments">
        <li v-for="row in mine" :key="row.adjustment_id">
          <span class="mono move">
            {{ issueLabel(row.from_code) }} → {{ row.to_code ? issueLabel(row.to_code) : '移出台账' }}
          </span>
          <span class="muted small why">{{ row.reason }}</span>
          <span class="muted small who">{{ row.actor }} · {{ formatDate(row.decided_at) }}</span>
          <button v-if="canAdjust" type="button" class="link" :disabled="busy"
                  @click="undo(row.adjustment_id)">撤销</button>
        </li>
      </ul>
    </section>

    <section class="card block">
      <h2>
        时间线（{{ issue.timeline.length }} 条）
        <button v-if="canAdjust && !adjusting" type="button" class="link head-action"
                @click="adjusting = true">改派事件…</button>
      </h2>

      <div v-if="notice" class="notice" :class="notice.tone">{{ notice.text }}</div>

      <form v-if="adjusting" class="reassign" @submit.prevent="submit">
        <p class="muted small">
          不勾选任何事件＝把整个专题并入目标（<b>合并</b>）；勾选一部分＝把它们分出去（<b>拆分</b>）。
        </p>
        <div class="row">
          <label>
            移到
            <Select v-model="draft.to_code" label="改派到" :options="targetOptions" />
          </label>
          <label class="grow">
            理由
            <input v-model="draft.reason" type="text" required minlength="4"
                   placeholder="说明这些事件不属于本专题的理由" />
          </label>
        </div>
        <div class="row">
          <span class="muted small">
            已选 {{ draft.event_ids.length || issue.timeline.length }} / {{ issue.timeline.length }} 条
          </span>
          <button type="submit" class="primary" :disabled="busy || draft.reason.length < 4">
            {{ draft.event_ids.length ? '拆分' : '整体合并' }}
          </button>
          <button type="button" class="link" :disabled="busy" @click="adjusting = false">取消</button>
        </div>
      </form>

      <ol class="timeline">
        <li v-for="entry in issue.timeline" :key="entry.event_id">
          <div v-if="adjusting" class="pick">
            <input type="checkbox" :checked="draft.event_ids.includes(entry.event_id)"
                   :aria-label="`选择「${entry.summary}」`" @change="toggleEvent(entry.event_id)" />
          </div>
          <div class="when">
            <span class="mono">{{ formatDate(entry.sort_time) }}</span>
            <span class="muted basis">{{ basisLabel(entry.time_basis) }}</span>
            <span v-if="entry.target_time" class="muted basis">· {{ targetText(entry.target_time) }}</span>
          </div>
          <div class="what">
            <RouterLink :to="`/events/${entry.event_id}`">{{ entry.summary }}</RouterLink>
            <div class="muted small">
              {{ entry.source_name || '—' }} · 风险 {{ entry.risk_score?.toFixed?.(0) ?? '—' }}
              <span v-if="entry.needs_review" class="tag review">需复核</span>
              <span v-if="entry.reassigned" class="tag hand">人工改派</span>
            </div>
          </div>
        </li>
      </ol>
    </section>
  </AsyncSection>
</template>

<style scoped>
.trail { list-style: none; margin: 0; padding: 0; }
.trail li { display: flex; gap: 14px; align-items: baseline; padding: 6px 0;
  border-top: 1px solid var(--gridline); font-size: var(--fs-aux); }
.trail li:first-child { border-top: none; }
.move { flex: none; display: flex; gap: 6px; align-items: baseline; }
.arrow { color: var(--text-muted); }
.why { min-width: 0; }
.basis { font-size: var(--fs-note); display: block; }
.head-action { float: right; font-weight: 400; text-transform: none; letter-spacing: 0; }
.adjustments { list-style: none; margin: 0; padding: 0; }
.adjustments li { display: flex; gap: 12px; align-items: baseline; padding: 6px 0;
  border-top: 1px solid var(--gridline); font-size: var(--fs-aux); }
.adjustments li:first-child { border-top: none; }
.adjustments .move { flex: none; }
.adjustments .why { flex: 1; min-width: 0; }
.adjustments .who { flex: none; }
.reassign { border: 1px solid var(--gridline); border-radius: 6px; padding: 10px 12px;
  margin-bottom: 12px; }
.reassign .row { display: flex; gap: 12px; align-items: flex-end; margin-top: 8px; }
.reassign label { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-note);
  color: var(--text-muted); }
.reassign .grow { flex: 1; }
.reassign input[type='text'], .reassign select { font: inherit; font-size: var(--fs-aux);
  padding: 4px 6px; border: 1px solid var(--gridline); border-radius: 4px;
  background: var(--surface-1); color: var(--text-primary); }
.pick { flex: none; padding-top: 2px; }
.tag.hand { border-color: var(--text-muted); color: var(--text-muted); }
.notice { font-size: var(--fs-aux); padding: 6px 10px; border-radius: 4px; margin-bottom: 10px;
  border: 1px solid var(--gridline); }
.notice.bad { border-color: var(--status-critical); color: var(--status-critical); }
h2 { font-size: var(--fs-aux); font-weight: 600; color: var(--text-muted); text-transform: uppercase;
  letter-spacing: 0.04em; margin: 0 0 10px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); gap: 12px; }
.block { margin-top: 12px; }
.timeline { list-style: none; margin: 0; padding: 0; }
.timeline li { display: flex; gap: 14px; padding: 9px 0; border-top: 1px solid var(--gridline); font-size: var(--fs-body); }
.timeline li:first-child { border-top: none; }
.when { flex: none; width: 92px; }
.what { min-width: 0; }
.tag.review { color: var(--status-serious); border-color: var(--status-serious); margin-left: 6px; }
</style>
