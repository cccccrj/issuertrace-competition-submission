<script setup>
import { computed, ref } from 'vue'
import { CORRECT_LABELS, JUDGEMENT_LABELS, METHOD_LABELS, PARTIAL_SUBTYPE_LABELS, TASK_KIND_LABELS, TASK_STATUS_LABELS, api, companyLabel, formatDate, issueLabel, labelText, reviewReasonLabel } from '../api.js'
import Select from './ui/Select.vue'
import NumericCheck from './NumericCheck.vue'
import VerdictBadge from './VerdictBadge.vue'

const props = defineProps({
  task: { type: Object, required: true },
  focused: { type: Boolean, default: false },
  writable: { type: Boolean, default: false },
  moderator: { type: Boolean, default: false },
})
const emit = defineEmits(['judge', 'approve', 'reject'])

const note = ref('')
const picking = ref(false)
const subtype = ref('')
const subtypeOptions = [
  { value: '', label: '部分支持的子类（可选）' },
  ...Object.entries(PARTIAL_SUBTYPE_LABELS).map(([value, label]) => ({ value, label })),
]
// Unset rather than false: "nobody has looked at the citation" and "the citation
// is wrong" are different facts, and only the second belongs in the rate.
const evidenceOk = ref(null)
const page = ref(null)
const pageError = ref(null)
const loadingPage = ref(false)

const target = computed(() => props.task.target)
const labels = computed(() => CORRECT_LABELS[props.task.kind] || [])
const latest = computed(() => [...props.task.decisions].reverse()
  .find((entry) => entry.action === 'judge') || null)

/** A judgement carries the two evaluation fields only where they apply: a right
 *  answer belongs to a "wrong", and evidence location only to a claim verdict. */
function submit(judgement, correctLabel = null) {
  const body = { judgement, note: note.value || null }
  if (judgement === 'wrong' && correctLabel) {
    body.correct_label = correctLabel === 'PARTIAL' && subtype.value
      ? `PARTIAL/${subtype.value}` : correctLabel
  }
  if (props.task.kind === 'verdict' && evidenceOk.value !== null) body.evidence_ok = evidenceOk.value
  picking.value = false
  subtype.value = ''
  emit('judge', props.task, body)
}

function judge(judgement) {
  // "Wrong" alone cannot produce a confusion matrix, so the reviewer is asked
  // for the right class — and can still skip it, which metrics then reports.
  if (judgement === 'wrong') picking.value = !picking.value
  else submit(judgement)
}

async function openPage() {
  if (page.value) { page.value = null; return }
  loadingPage.value = true
  pageError.value = null
  try {
    page.value = await api.evidencePage(target.value.evidence_version_id, target.value.evidence_page)
  } catch (exception) {
    pageError.value = exception.message
  } finally {
    loadingPage.value = false
  }
}

defineExpose({ judge, wrongWithLabel: (label) => submit('wrong', label) })
</script>

<template>
  <article class="card task" :class="{ focused }">
    <header class="head">
      <div>
        <span class="tag">{{ TASK_KIND_LABELS[task.kind] || task.kind }}</span>
        <span class="tag">{{ task.origin_label }}</span>
        <span class="muted small">{{ companyLabel(task.company_id) }}</span>
        <!-- 分派只是工作清单，不是权限：服务端不会拦下别人那一条。写在卡片上，
             是为了让「顺手判了同事那份」是个看得见的选择,而不是一次意外。 -->
        <span v-if="task.assignees?.length" class="tag owner">
          {{ task.assignees.length > 1 ? '双标' : '分派' }} {{ task.assignees.join(' · ') }}
        </span>
      </div>
      <span class="status" :class="task.status">{{ TASK_STATUS_LABELS[task.status] || task.status }}</span>
    </header>

    <!-- 主张裁决：媒体原句 / 权威证据 / 系统裁决 三栏并排，人工判定在下方。 -->
    <div v-if="task.kind === 'verdict' && target" class="compare">
      <section>
        <h3>媒体原句</h3>
        <p>{{ target.text }}</p>
        <div class="muted small">
          {{ issueLabel(target.issue_code) }} ·
          <RouterLink :to="`/events/${target.event_id}`">来源事件 →</RouterLink>
        </div>
      </section>
      <section>
        <h3>权威证据</h3>
        <template v-if="target.evidence_page">
          <div class="muted small">
            <a v-if="target.evidence_url"
               :href="`${target.evidence_url}#page=${target.evidence_page}`"
               target="_blank" rel="noopener">《{{ target.evidence_title }}》</a>
            <span v-else>《{{ target.evidence_title }}》</span>
            第 {{ target.evidence_page }} 页 · {{ target.evidence_section }}
          </div>
          <p>{{ target.evidence_text }}</p>
          <!-- 两条路含义不同：外链去发布方的原件（可能打不开），站内看的是平台
               索引到的那一页（一定打得开，但是副本）。 -->
          <button class="link" :disabled="loadingPage" @click="openPage">
            {{ page ? '收起本页原文' : loadingPage ? '载入中…' : '展开本页原文' }}
          </button>
          <a v-if="target.evidence_url" class="link"
             :href="`${target.evidence_url}#page=${target.evidence_page}`"
             target="_blank" rel="noopener">去原网页 →</a>
          <div v-if="pageError" class="muted small">{{ pageError }}</div>
          <div v-if="page" class="page-text">
            <p v-for="paragraph in page.paragraphs" :key="paragraph.paragraph_id">{{ paragraph.text }}</p>
            <p v-if="!page.paragraphs.length" class="muted small">该页没有已索引的段落。</p>
          </div>
        </template>
        <p v-else class="muted small">该裁决没有引用证据（{{ METHOD_LABELS[target.method] || target.method }}）。</p>
      </section>
      <section>
        <h3>系统裁决</h3>
        <VerdictBadge :verdict="target.verdict" :subtype="target.partial_subtype" />
        <p class="muted small">{{ target.reason }}</p>
        <NumericCheck :check="target.numeric_check" />
      </section>
    </div>

    <div v-else-if="task.kind === 'event' && target" class="compare">
      <section>
        <h3>事件摘要</h3>
        <p>{{ target.summary }}</p>
        <div class="muted small">
          <RouterLink :to="`/events/${task.target_id}`">{{ target.title }}</RouterLink>
          <a v-if="target.canonical_url" :href="target.canonical_url"
             target="_blank" rel="noopener">原文 →</a>
          · {{ formatDate(target.published_at) }}
        </div>
      </section>
      <section>
        <h3>标记原因</h3>
        <!-- 此前这里把 low_confidence 这类英文码原样 join('、') 打给复核员。 -->
        <p>{{ (target.review_reasons || []).map(reviewReasonLabel).join('、') || '—' }}</p>
      </section>
      <section>
        <h3>风险等级</h3>
        <p>{{ target.risk_level }}</p>
      </section>
    </div>

    <div v-else-if="task.kind === 'diff' && target" class="compare">
      <section>
        <h3>变更前</h3>
        <p>{{ target.old_value || target.left_text || '—' }}</p>
        <div class="muted small">第 {{ target.left_page }} 页</div>
      </section>
      <section>
        <h3>变更后</h3>
        <p>{{ target.new_value || target.right_text || '—' }}</p>
        <div class="muted small">第 {{ target.right_page }} 页</div>
      </section>
      <section>
        <h3>版本与章节</h3>
        <p>{{ target.pair_key }}</p>
        <div class="muted small">{{ target.section }} · {{ target.diff_kind }}</div>
      </section>
    </div>

    <div v-else class="muted small">该任务指向的对象已不在当前数据中。</div>

    <!-- 双标任务上，另一个人的判定要等你判完才出现。两份判定必须各判各的，
         否则一致性量的是谁先开口，不是判据是否一致。 -->
    <div v-if="task.blinded" class="log blinded muted small">
      本任务为双人标注，另一位复核人已完成判定，其结果将在您提交判定后显示。
    </div>
    <div v-if="task.decisions.length" class="log">
      <div v-for="entry in task.decisions" :key="entry.decision_id" class="log-line muted small">
        {{ entry.decided_at.slice(0, 16).replace('T', ' ') }} ·
        {{ entry.reviewer }}（{{ entry.role }}）
        {{ entry.action === 'judge' ? JUDGEMENT_LABELS[entry.judgement] || entry.judgement
           : entry.action === 'approve' ? '通过' : '退回' }}
        <span v-if="entry.correct_label">→ 应为 {{ labelText(task.kind, entry.correct_label) }}</span>
        <span v-if="entry.evidence_ok !== null && entry.evidence_ok !== undefined">
          · 证据定位{{ entry.evidence_ok ? '正确' : '错误' }}</span>
        <span v-if="entry.note">· {{ entry.note }}</span>
      </div>
    </div>

    <template v-if="writable">
      <!-- 证据定位与四值判定是两个指标（§11.3），因此分开记录，不合成一个对错。 -->
      <div v-if="task.kind === 'verdict' && target?.evidence_page" class="evidence-ask">
        <span class="muted small">引用的页码对吗？</span>
        <button :class="{ on: evidenceOk === true }" @click="evidenceOk = evidenceOk === true ? null : true">对</button>
        <button :class="{ on: evidenceOk === false }" @click="evidenceOk = evidenceOk === false ? null : false">错</button>
        <span class="muted small">（不选则不计入证据定位准确率）</span>
      </div>

      <div class="actions">
        <input v-model="note" type="search" placeholder="判定说明（可选）" aria-label="判定说明（可选）" />
        <button v-for="(label, key) in JUDGEMENT_LABELS" :key="key"
                :class="{ armed: key === 'wrong' && picking }" @click="judge(key)">
          {{ label }}
          <span class="key mono">{{ { correct: '1', wrong: '2', unsure: '3' }[key] }}</span>
        </button>
      </div>

      <div v-if="picking" class="picker">
        <span class="muted small">请选择正确取值（用于计算混淆矩阵）；无法确定时可跳过</span>
        <div class="picker-row">
          <Select
            v-if="task.kind === 'verdict'" v-model="subtype"
            label="部分支持的子类" :options="subtypeOptions" />
          <button v-for="label in labels" :key="label" @click="submit('wrong', label)">
            {{ labelText(task.kind, label) }}
          </button>
          <button class="plain" @click="submit('wrong')">跳过，只记「错」</button>
        </div>
      </div>

      <div v-if="moderator" class="actions">
        <button @click="emit('approve', task)">通过</button>
        <button @click="emit('reject', task, note || null)">退回</button>
      </div>
    </template>
    <div v-else class="muted small">当前角色只读，无法提交判定。</div>
    <div v-if="latest" class="muted small verdict-line">
      最近判定：{{ JUDGEMENT_LABELS[latest.judgement] || latest.judgement }}
    </div>
  </article>
</template>

<style scoped>
.task { margin-bottom: 12px; }
.task.focused { border-color: var(--accent, var(--status-good)); box-shadow: 0 0 0 1px var(--accent, var(--status-good)); }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 10px; }
.tag.owner { color: var(--text-secondary); }
.blinded { border-left: 2px solid var(--border); padding-left: 8px; }
.status { font-size: var(--fs-note); color: var(--text-secondary); }
.status.open { color: var(--status-serious); }
.status.approved { color: var(--status-good); }
.status.rejected { color: var(--status-critical); }
.compare { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr)); gap: 14px; }
.compare section { border-left: 2px solid var(--gridline); padding-left: 11px; min-width: 0; }
.compare h3 { font-size: var(--fs-note); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--text-muted); margin: 0 0 6px; }
.compare p { font-size: var(--fs-body); margin: 0 0 5px; line-height: 1.7; }
/* 这是被索引到的整页原文，与主张下的引证是同一种东西，所以戴同一个记号：
   内嵌底 + 蓝色左线。此前它是灰左线——和「被丢弃、定位不到的原话」用的是同一
   种灰线，而那一处恰恰要表达的是「这不是原文」。 */
.page-text { margin-top: var(--sp-2); max-height: 260px; overflow-y: auto;
  border-left: 2px solid var(--series-1); background: var(--surface-2);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  padding: var(--sp-2) 0 var(--sp-2) var(--sp-3); }
.page-text p { font-size: var(--fs-aux); color: var(--text-secondary); }
button.link { background: none; border: none; padding: 0; color: var(--text-secondary);
  text-decoration: underline; cursor: pointer; font: inherit; font-size: var(--fs-note); }
.log { margin-top: 12px; border-top: 1px solid var(--gridline); padding-top: 8px; }
.log-line { padding: 2px 0; }
.actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
.actions input { flex: 1; min-width: 180px; }
.actions button.armed { border-color: var(--status-warning); }
.key { font-size: var(--fs-note); color: var(--text-muted); margin-left: 4px; }
.evidence-ask { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
.evidence-ask button.on { border-color: var(--status-good); color: var(--text-primary); }
.picker { margin-top: 10px; border-top: 1px dashed var(--gridline); padding-top: 8px; }
.picker-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
.picker-row button.plain { color: var(--text-muted); }
.verdict-line { margin-top: 6px; }
</style>
