<script setup>
/**
 * 风险研判工作台 — 这套平台里最重要的一页。
 *
 * 此前它叫「研判档案」：六段内容可读，能写的只有「对外口径」和「下一步状态」两样，
 * 说不出这条风险**成不成立**，上报之后也说不出**轮到谁**。修改思路 v2 §四—§六把
 * 它改成一张工作台：左边是依据，右边一栏「人工研判」是这一页存在的目的。
 * 「档案」留给归档后的结果，不再用来叫一件还在处理中的事。
 *
 * 左边四段用锚点而不是 JS 切页签：报告要能打印，而且四段内容全在 DOM 里，对搜索、
 * 读屏和 Ctrl+F 都更好。四段是原来六段合并出来的——证据核验与多源信息同属「凭什么」，
 * 传播、披露变化与相似事项同属「前后左右发生了什么」。
 *
 * 命题要求的七项输出（摘要、主体、影响对象、可信度、证据来源、时间线、预警理由）
 * 在这里都有，但编号 ①–⑦ 留在 `/events/:id`——那组编号是与命题输出要求的契约，
 * 不是版式选择，所以它不跟着业务化改名走。本页在研判概览里给一条直达链接。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  CREDIBILITY_SHORT, DISPOSITION_STATUS_META, PRODUCTION_LABELS, RISK_LEVEL_LABELS, api,
  companyLabel, formatDate, impactObjectLabel, issueLabel, tierLabel, METHOD_LABELS, DIFF_KIND_LABELS } from '../api.js'
import { allows, session } from '../session.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import DataTable from '../components/ui/DataTable.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import AssistantPanel from '../components/AssistantPanel.vue'
import Select from '../components/ui/Select.vue'
import EvidenceCite from '../components/EvidenceCite.vue'
import LevelBadge from '../components/LevelBadge.vue'
import TechDetails from '../components/TechDetails.vue'
import VerdictBadge from '../components/VerdictBadge.vue'
import VerdictBar from '../components/VerdictBar.vue'

const route = useRoute()
const data = ref(null)
const loading = ref(true)
const error = ref('')

/* 四段的顺序是固定的：先说结论，再说依据，再说前后左右，最后才是「后来怎么办的」。 */
const SECTIONS = [
  { id: 'overview', label: '研判概览' },
  { id: 'evidence', label: '证据与来源' },
  { id: 'context', label: '关联与脉络' },
  { id: 'history', label: '处置与记录' },
]

const SOURCE_GROUPS = [
  { key: 'authoritative', title: '权威披露', note: '交易所、监管与公司正式文件——事实基座。' },
  { key: 'independent', title: '独立报道', note: '非转载的主流与行业媒体，提供线索。' },
  { key: 'relay', title: '传播与转述', note: '转载、改写、简讯与合作稿，不另计为独立信源。' },
]

// 来源层级的大白话，悬停可见：S2 本身对读者什么也没说。
const TIER_HINTS = {
  S0: 'S0 · 交易所、监管与公司正式披露', S1: 'S1 · 官方与权威机构发布',
  S2: 'S2 · 独立媒体、行业媒体与专业数据库', S3: 'S3 · 转载、社交媒体与其他线索',
}

/** 标题读不出时用网址里的路径兜底，而不是写「（无标题）」：网址至少说得出是哪一篇。 */
function sourceTitle(item) {
  if (item.title) return item.title
  if (!item.canonical_url) return '（无标题）'
  try {
    const url = new URL(item.canonical_url)
    const slug = decodeURIComponent(url.pathname.split('/').filter(Boolean).pop() || '')
    return slug ? `${url.hostname.replace(/^www\./, '')}/…/${slug}` : url.hostname
  } catch { return item.canonical_url }
}

const SPREAD_ACTIONS = {
  ORIGIN: '首发', REPOST: '转载', REWRITE: '改写', RECURRING: '同源重复',
  FROM_DISCLOSURE: '源于披露', RESPONSE: '回应', FOLLOW_UP: '跟进报道',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.risk(route.params.id)
    // 从「我的任务」点进来带着 #decide：内容是异步到的，路由那一刻锚点还不存在，
    // 等渲染完再滚过去——窄屏时右栏排在最下面，不滚就看不见。
    if (route.hash === '#decide') {
      await nextTick()
      document.getElementById('decide')?.scrollIntoView?.({ block: 'start' })
    }
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}
watch(() => route.params.id, load)
load()

const grouped = computed(() => Object.fromEntries(
  SOURCE_GROUPS.map((group) => [group.key,
    (data.value?.sources || []).filter((item) => item.group === group.key)])))

const verdictCounts = computed(() => {
  const counts = {}
  for (const item of data.value?.evidence || []) {
    counts[item.verdict] = (counts[item.verdict] || 0) + 1
  }
  return counts
})

/* ——— 人工研判 ——————————————————————————————————————————————
   三块：AI 判断（只读）、人工结论（写结论、定级、意见）、下一步（走流程）。
   合法去向、所需角色、结论可选值都由服务端随档案返回；这里不再实现一遍流程。
   无权限的动作照常列出并写明原因——隐藏一项能力和没有这项能力，在页面上长得一模一样。 */
const status = computed(() => data.value?.disposition.status || 'pending')
/* 待上级复核的事项在上级手里：提交人看得见，改不了。服务端同样拦着（403）。 */
const inReview = computed(() => status.value === 'reporting')
const canJudge = computed(() => allows('analyst') && status.value !== 'archived'
  && (!inReview.value || allows('reviewer')))

const judgement = ref({ conclusion: '', level_override: '', opinion: '' })
const step = ref({ target: '', chosen: '', owner: '', note: '', due_at: '' })
const saving = ref(false)
const decideError = ref('')
const saved = ref('')

watch(data, (value) => {
  if (!value) return
  judgement.value = {
    conclusion: value.disposition.conclusion || '',
    level_override: value.disposition.level_override || '',
    opinion: value.opinion || '',
  }
  step.value = { target: '', chosen: value.disposition.chosen || '', owner: '', note: '',
    due_at: value.disposition.due_at || '' }
})

const CONCLUSION_OPTIONS = computed(() => [
  { value: '', label: '— 选择人工结论 —' },
  ...Object.entries(data.value?.conclusions || {}).map(([value, meta]) => ({ value, label: meta.label })),
])
const LEVEL_OPTIONS = computed(() => [
  { value: '', label: `跟随系统（${RISK_LEVEL_LABELS[data.value?.risk_level] || '—'}）` },
  ...['high', 'medium', 'low'].map((value) => ({ value, label: RISK_LEVEL_LABELS[value] })),
])
/* 对外口径是可选的：不选就不动。此前它默认填系统建议，于是「认领」也会被记成
   「人选了系统建议的口径」。 */
const ACTION_OPTIONS = computed(() => [
  { value: '', label: '暂不选定口径' },
  ...['cold', 'formal_reply', 'clarification', 'escalate'].map((value) => ({
    value,
    label: { cold: '冷处理', formal_reply: '正式回复', clarification: '澄清公告', escalate: '上报项目组' }[value]
      + (value === data.value?.disposition.recommended ? '（系统建议）' : ''),
  })),
])

/* 动作按钮说的是「做什么」，不是「变成什么状态」：同一个目标状态在不同位置读法不同——
   从退回补证再上报，是「补证后再次上报」。 */
const VERBS = {
  investigating: '发起专项核查', reporting: '上报复核', tracking: '加入持续跟踪',
  archived: '确认归档', returned: '退回补证',
}
const verb = (target) => (target === 'reporting' && status.value === 'returned'
  ? '补证后再次上报' : VERBS[target] || DISPOSITION_STATUS_META[target]?.label)

const allowed = (transition) => !transition.requires || allows(transition.requires)
const picked = computed(() => data.value?.transitions.find((t) => t.status === step.value.target) || null)

/* 各去向要带上的东西。只列这一步真正要的：上报要交给谁、为什么；退回要说补什么；
   跟踪要说什么时候再看。 */
const needs = computed(() => ({
  owner: ['reporting', 'investigating'].includes(step.value.target),
  reviewer: step.value.target === 'reporting',
  due: ['reporting', 'investigating', 'tracking'].includes(step.value.target),
  noteRequired: step.value.target === 'returned',
}))
const NOTE_HINTS = {
  reporting: '上报理由与需上级确认的问题',
  investigating: '核查目标与要查清的问题',
  tracking: '跟踪什么、出现什么情况再处理',
  returned: '退回意见：要补什么证据（必填）',
  archived: '归档说明（可留空）',
}
const DUE_LABELS = { reporting: '复核截止', investigating: '核查截止', tracking: '下次复核' }

const reviewers = ref([])
watch(() => step.value.target, async (target) => {
  if (target === 'investigating' && !step.value.owner) step.value.owner = session.username || ''
  if (target === 'reporting' && !reviewers.value.length) {
    try {
      reviewers.value = (await api.reviewers()).items
    } catch {
      reviewers.value = []
    }
    if (!step.value.owner && reviewers.value.length) step.value.owner = reviewers.value[0].username
  }
})
const REVIEWER_OPTIONS = computed(() => reviewers.value.map((person) => ({
  value: person.username, label: `${person.username}（${person.role_label}）` })))

/* 上报前要有结论——表单里填了还没存也算，随这一步一起送出去。 */
const stepProblem = computed(() => {
  if (step.value.target === 'reporting' && status.value !== 'reporting' && !judgement.value.conclusion) {
    return '上报前先在上方给出人工结论。'
  }
  if (needs.value.noteRequired && !step.value.note.trim()) return '退回必须写明退回意见。'
  if (needs.value.reviewer && !step.value.owner) return '选择上报给哪位核查负责人。'
  return ''
})

async function send(body) {
  saving.value = true
  decideError.value = ''
  saved.value = ''
  try {
    await api.notifyDecide(route.params.id, body)
    await load()
    return true
  } catch (exception) {
    decideError.value = exception.message
    return false
  } finally {
    saving.value = false
  }
}

function judged() {
  return judgement.value.conclusion ? {
    conclusion: judgement.value.conclusion,
    level_override: judgement.value.level_override || null,
    opinion: judgement.value.opinion.trim() || null,
  } : {}
}

/* 三个结论按钮都停在当前状态：写结论不等于走下一步。 */
async function saveJudgement(kind) {
  if (kind === 'confirm') judgement.value = { ...judgement.value, conclusion: 'confirmed', level_override: '' }
  if (kind === 'false_positive') judgement.value = { ...judgement.value, conclusion: 'false_positive' }
  if (!judgement.value.conclusion) {
    decideError.value = '先选择人工结论。'
    return
  }
  if (await send({ status: status.value, ...judged() })) saved.value = '研判结论已保存。'
}

async function claim() {
  if (await send({ status: status.value, owner: session.username })) saved.value = '已认领，负责人是你。'
}

async function go() {
  if (!picked.value || !allowed(picked.value) || stepProblem.value) return
  const body = { status: step.value.target, note: step.value.note.trim() || null, ...judged() }
  if (step.value.chosen) body.chosen = step.value.chosen
  if (needs.value.owner && step.value.owner) body.owner = step.value.owner
  // 空串就是「不设/清空」。框里默认填的是已有的截止日，没动过就原样送回，期限不会被抹掉。
  if (needs.value.due) body.due_at = step.value.due_at || null
  if (await send(body)) saved.value = `已${verb(step.value.target)}。`
}
</script>

<template>
  <PageHeader
    title="风险研判工作台"
    note="在本页查看依据、填写研判结论并确定后续处置。"
    :crumbs="[{ label: '风险事项库', to: '/risks' }]">
    <template #actions>
      <!-- 直接下载，不经过 /report 那个打印页：事件与议题的报告页是为「打印成
           PDF」存在的，而简报要的是一份能发出去的 Word。 -->
      <a v-if="data" :href="`/api/reports/risks/${data.alert_id}.docx`">导出风险简报 →</a>
    </template>
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="!data"
                empty-text="找不到这条风险事项。它可能已被重算替换，回风险事项库重新选一条。">
    <!-- 事项总览：结论、状态、谁在管。往下翻时右栏仍在，这一块不必跟着。 -->
    <section class="card summary-card">
      <h2 class="subject">{{ data.headline }}</h2>
      <div class="facts">
        <LevelBadge :level="data.risk_level" :score="data.adjusted_risk_score" />
        <span v-if="data.disposition.level_override && data.disposition.level_override !== data.risk_level"
              class="tag override">人工定级：{{ RISK_LEVEL_LABELS[data.disposition.level_override] }}</span>
        <span class="state" :style="{ color: DISPOSITION_STATUS_META[data.disposition.status]?.color }">
          <span aria-hidden="true">{{ DISPOSITION_STATUS_META[data.disposition.status]?.icon }}</span>
          {{ data.disposition.status_label }}
        </span>
        <span v-if="data.disposition.conclusion_label" class="tag"
              :class="{ fp: data.disposition.false_positive }">
          人工结论：{{ data.disposition.conclusion_label }}
        </span>
        <RouterLink :to="`/companies/${data.company_id}`" class="company">{{ companyLabel(data.company_id) }}</RouterLink>
        <span v-if="data.issue_code" class="tag">{{ issueLabel(data.issue_code) }}</span>
        <span class="muted small">负责人：{{ data.disposition.owner || '未指派' }}</span>
        <span v-if="data.disposition.due_at" class="small" :class="data.disposition.overdue ? 'late' : 'muted'">
          截止 {{ data.disposition.due_at }}<template v-if="data.disposition.overdue"> · 已逾期</template>
        </span>
      </div>
      <p class="why">
        预警理由：{{ data.reasons.map((reason) => reason.label).join('；') || '—' }}
      </p>
      <p class="muted small">
        窗口 {{ formatDate(data.window_start) }} — {{ formatDate(data.window_end) }} ·
        合并 {{ data.event_count }} 个事件 ·
        事件可信度 {{ data.credibility?.toFixed?.(2) }}（{{ CREDIBILITY_SHORT[data.credibility_label] || '—' }}）·
        最后更新 {{ formatDate(data.latest_progress) }}
      </p>
    </section>

    <!-- 退回补证时，提交人一打开就要看到上级说了什么，不必翻到最底下的流水里去找。 -->
    <Callout v-if="data.returned_by" tone="warn" class="returned">
      <b>上级退回意见</b>（{{ data.returned_by.actor }} · {{ formatDate(data.returned_by.occurred_at) }}）：
      {{ data.returned_by.note }}
    </Callout>

    <!-- 研判助手放在总览之下、工作台之上：读完结论，最自然的下一句就是「凭什么」。 -->
    <AssistantPanel scope="risk" :object-id="data.alert_id" />

    <div class="workbench">
      <div class="sheets">
        <nav class="anchors" aria-label="工作台分段">
          <a v-for="section in SECTIONS" :key="section.id" :href="`#${section.id}`">{{ section.label }}</a>
        </nav>

        <!-- ——— 一 · 研判概览 ——————————————————————————————————— -->
        <section id="overview" class="card">
          <h2 class="section">研判概览</h2>
          <p class="prose">{{ data.summary }}</p>
          <dl class="detail-grid">
            <dt>事件主体</dt>
            <dd>{{ data.subjects.join('、') || '—' }}</dd>
            <dt>影响对象</dt>
            <dd>{{ data.affected_objects.map(impactObjectLabel).join('、') || '—' }}</dd>
            <dt>披露阶段</dt>
            <dd>{{ data.phase_label }}（系数 {{ data.phase_coefficient }}）</dd>
            <dt>人工结论</dt>
            <dd>
              {{ data.disposition.conclusion_label || '尚未给出' }}
              <span v-if="data.opinion" class="muted small"> · {{ data.opinion }}</span>
            </dd>
          </dl>

          <Callout tone="info">
            系统研判仅供辅助排序与提示，最终以公开披露及人工核查结论为准。事件可信度衡量的是
            这条信息本身的证据强度，与系统对自身抽取结果的置信度是两个数，不合成一个。
          </Callout>

          <div class="cross-links">
            <RouterLink class="cross" :to="`/events/${data.lead_event_id}`">
              查看首发事件的完整分析 →
            </RouterLink>
            <!-- 主体、影响对象、方向属于事件，不属于事项：一个事项合并了多个事件，在这里改
                 不知道改的是哪一个。判断模型标得对不对，是标注复核的事。 -->
            <RouterLink class="muted small" to="/review">对系统研判有异议 → 标注复核</RouterLink>
          </div>
        </section>

        <!-- ——— 二 · 证据与来源 ——————————————————————————————————— -->
        <section id="evidence" class="card">
          <h2 class="section">证据与来源</h2>
          <h3 class="sub">核验点</h3>
          <p class="page-note">
            媒体表述逐条与权威披露原文比对。每条结论都指向某份文件的第几页；没有页码的结论
            不会被写入。
          </p>
          <VerdictBar v-if="data.evidence.length" :counts="verdictCounts"
                      :total="data.evidence.length" />
          <div v-for="(item, index) in data.evidence" :key="index" class="check">
            <div class="check-head">
              <VerdictBadge :verdict="item.verdict" :subtype="item.partial_subtype" />
              <span class="claim">{{ item.claim }}</span>
            </div>
            <p v-if="item.reason" class="muted small reason">{{ item.reason }}</p>
            <EvidenceCite
              v-if="item.evidence_title"
              :title="item.evidence_title" :url="item.evidence_url"
              :page="item.evidence_page" :section="item.evidence_section"
              :text="item.evidence_excerpt" :note="item.method_label || METHOD_LABELS[item.method] || ''" />
          </div>
          <p v-if="!data.evidence.length" class="empty">
            这条事项没有可定位的关键事实核验点，因此不计入核验成功率的分母。
          </p>

          <p class="page-note sources-note">
            聚合不等于删除：转载与改写仍然逐条列出，只是不另计为独立信源。标题直达原文。
          </p>
          <!-- 结论立在什么上面（I-05）。「权威披露 0 篇」只说来源文章里没有公告；
               事实是否对过披露原文，要看上面的核验点。两种情况必须分开说。 -->
          <Callout v-if="data.source_basis?.kind === 'secondhand_only'" tone="warn" class="basis-note">
            <b>本事项完全依据二手报道。</b>来源中没有交易所、监管或公司公告，
            亦无任何事实经权威披露原文核对；风险分仅反映媒体报道情况，结论需另行取证。
          </Callout>
          <Callout v-else-if="data.source_basis?.kind === 'secondhand_checked'" class="basis-note">
            来源均为媒体报道（{{ data.source_basis.media_sources }} 篇），其中
            <b>{{ data.source_basis.checked_claims }} 条事实已与权威披露原文核对</b>
            （{{ data.source_basis.filings_cited.slice(0, 3).map((t) => `《${t}》`).join('、') }}<template
              v-if="data.source_basis.filings_cited.length > 3"> 等 {{ data.source_basis.filings_cited.length }} 份</template>，见上方核验点）。
          </Callout>
          <div v-for="group in SOURCE_GROUPS" :key="group.key" class="group">
            <h3 class="sub">{{ group.title }}<span class="muted small"> · {{ grouped[group.key].length }} 篇</span></h3>
            <p class="muted small">{{ group.note }}</p>
            <ul v-if="grouped[group.key].length" class="sources">
              <li v-for="item in grouped[group.key]" :key="item.version_id">
                <a v-if="item.canonical_url" :href="item.canonical_url" target="_blank" rel="noopener">
                  {{ sourceTitle(item) }}
                </a>
                <span v-else>{{ sourceTitle(item) }}</span>
                <span class="muted small">
                  · {{ item.source_name || '—' }}<span v-if="item.domain" class="mono"> · {{ item.domain }}</span>
                  · <span :title="TIER_HINTS[item.source_tier]">{{ tierLabel(item.source_tier) }}</span> ·
                  {{ PRODUCTION_LABELS[item.production_mode] || item.production_mode }} ·
                  {{ formatDate(item.published_at) }}
                  <span v-if="item.new_fact_count"> · 新增事实 {{ item.new_fact_count }} 处</span>
                </span>
              </li>
            </ul>
            <p v-else class="muted small">{{ group.key === 'authoritative'
              ? '来源文章里没有交易所、监管或公司公告。' : '这一类没有来源。' }}</p>
          </div>
        </section>

        <!-- ——— 三 · 关联与脉络 ——————————————————————————————————— -->
        <section id="context" class="card">
          <h2 class="section">关联与脉络</h2>

          <h3 class="sub">传播时间线</h3>
          <p class="page-note">请按时间先后顺序阅读，时间顺序是本部分最重要的信息。</p>
          <ol v-if="data.propagation.nodes.length" class="spread">
            <li v-for="node in data.propagation.nodes" :key="node.version_id">
              <span class="mono when">{{ formatDate(node.published_at) }}</span>
              <span class="tag">{{ SPREAD_ACTIONS[node.action] || node.action }}</span>
              <a v-if="node.canonical_url" :href="node.canonical_url" target="_blank" rel="noopener">
                {{ node.title || '（无标题）' }}
              </a>
              <span v-else>{{ node.title || '（无标题）' }}</span>
              <span class="muted small"> · {{ node.source_name || '—' }}</span>
            </li>
          </ol>
          <p v-else class="empty">这条事项的报道没有被转载或改写，传播链上只有它自己。</p>
          <TechDetails
            v-if="data.propagation.edges.length"
            hint="关系边"
            :items="[
              { label: '连通块', value: data.propagation.components.length },
              { label: '关系边', value: data.propagation.edges.length },
            ]" />

          <h3 class="sub">相似事项与历史处置</h3>
          <p class="page-note">
            同一专题或主要理由相同的其他事项及其历史处置结论。每条均注明相似理由，
            不提供相似度分值，以便核验与质疑。
          </p>
          <ul v-if="data.similar.length" class="similar">
            <li v-for="item in data.similar" :key="item.alert_id">
              <RouterLink :to="`/risks/${item.alert_id}`">{{ item.headline }}</RouterLink>
              <span class="muted small">
                · {{ companyLabel(item.company_id) }} · {{ RISK_LEVEL_LABELS[item.risk_level] || '—' }}
                · {{ item.disposition.status_label }}
                <template v-if="item.disposition.conclusion_label"> · 结论：{{ item.disposition.conclusion_label }}</template>
              </span>
              <span class="basis-line small">{{ item.basis.join('；') }}</span>
            </li>
          </ul>
          <p v-else class="empty">库里没有同专题或同理由的其他事项。</p>

          <h3 class="sub">披露变化</h3>
          <p class="page-note">同一份权威文件不同版本之间的关键变化，每条都带原文页码。</p>
          <DataTable
            v-if="data.changes.length"
            :columns="[
              { key: 'diff_kind', title: '类型', slot: 'kind', nowrap: true, width: '78px' },
              { key: 'section', title: '章节' },
              { key: 'old_value', title: '原值', slot: 'old', nowrap: true },
              { key: 'new_value', title: '新值', slot: 'new', nowrap: true },
              { key: 'right_page', title: '页码', nowrap: true, width: '64px' },
            ]"
            :rows="data.changes"
            row-key="diff_id"
          >
            <template #kind="{ row }">{{ DIFF_KIND_LABELS[row.diff_kind] || '其他' }}</template>
            <template #old="{ row }"><span class="mono">{{ row.old_value ?? '—' }}</span></template>
            <template #new="{ row }"><span class="mono">{{ row.new_value ?? '—' }}</span></template>
          </DataTable>
          <p v-else class="empty">这家公司没有可比对的多版本披露文件，因此没有版本变化可查。</p>

          <RouterLink v-if="data.issue_id" class="cross" :to="`/issues/${data.issue_id}`">
            查看所属专题的完整时间线 →
          </RouterLink>
        </section>

        <!-- ——— 四 · 处置与记录 ——————————————————————————————————— -->
        <section id="history" class="card">
          <h2 class="section">处置与记录</h2>
          <p class="page-note">
            逐步记录操作人、时间、状态变更、结论与说明，涵盖上报、退回、
            核查、跟踪与归档全过程。
          </p>
          <ol v-if="data.history.length" class="history">
            <li v-for="entry in data.history" :key="entry.event_seq">
              <span class="mono when">{{ formatDate(entry.occurred_at) }}</span>
              <b>{{ entry.actor }}</b>
              <span v-if="entry.from_status === entry.to_status">{{ entry.to_label }} · 更新</span>
              <span v-else>{{ entry.from_label || '—' }} → {{ entry.to_label }}</span>
              <span v-if="entry.owner" class="muted small">负责人 {{ entry.owner }}</span>
              <span v-if="entry.conclusion_label" class="tag">{{ entry.conclusion_label }}</span>
              <span v-if="entry.chosen_label" class="tag">{{ entry.chosen_label }}</span>
              <span v-if="entry.note" class="muted small">{{ entry.note }}</span>
            </li>
          </ol>
          <p v-else class="empty">该事项暂无处置记录，可通过右侧「人工研判」记录首个步骤。</p>
        </section>
      </div>

      <!-- ——— 人工研判 ——————————————————————————————————————————— -->
      <aside id="decide" class="panel card" aria-label="人工研判">
        <h2 class="section">人工研判</h2>

        <div class="block">
          <h3 class="block-title">系统判断</h3>
          <dl class="mini">
            <dt>风险等级</dt>
            <dd><LevelBadge :level="data.risk_level" :score="data.adjusted_risk_score" /></dd>
            <dt>事件可信度</dt>
            <dd>{{ data.credibility?.toFixed?.(2) }}（{{ CREDIBILITY_SHORT[data.credibility_label] || '—' }}）</dd>
            <dt>口径建议</dt>
            <dd>{{ data.disposition.recommended_label || '—' }}</dd>
          </dl>
          <details v-if="data.recommended_basis.length" class="basis">
            <summary>建议依据</summary>
            <dl class="mini">
              <template v-for="item in data.recommended_basis" :key="item.code">
                <dt>{{ item.label }}</dt>
                <dd>{{ item.value }}</dd>
              </template>
            </dl>
          </details>
        </div>

        <div class="block">
          <h3 class="block-title">人工结论</h3>
          <button v-if="!data.disposition.owner && status === 'pending' && allows('analyst')"
                  type="button" class="move claim-me" :disabled="saving" @click="claim">
            由我研判（认领）
          </button>
          <label class="field-label">结论</label>
          <Select v-model="judgement.conclusion" label="人工结论" :options="CONCLUSION_OPTIONS"
                  :disabled="!canJudge" />
          <label class="field-label">人工定级</label>
          <Select v-model="judgement.level_override" label="人工定级" :options="LEVEL_OPTIONS"
                  :disabled="!canJudge" />
          <label class="field-label" for="opinion">研判意见</label>
          <textarea id="opinion" v-model="judgement.opinion" rows="3" :disabled="!canJudge"
                    placeholder="请填写人工研判依据、风险判断、需关注的问题或后续建议。" />
          <div class="row">
            <button type="button" class="move" :disabled="saving || !canJudge"
                    title="结论记为「风险成立」，定级跟随系统" @click="saveJudgement('confirm')">确认系统判断</button>
            <button type="button" class="move" :disabled="saving || !canJudge"
                    @click="saveJudgement('edit')">修改并确认</button>
            <button type="button" class="move quiet" :disabled="saving || !canJudge"
                    @click="saveJudgement('false_positive')">标记误报</button>
          </div>
          <p v-if="inReview && !allows('reviewer')" class="muted small">
            事项已上报，正等 {{ data.disposition.owner }} 复核；复核前结论不能再改。
          </p>
        </div>

        <div class="block">
          <h3 class="block-title">下一步</h3>
          <div v-if="data.transitions.length" class="steps" role="radiogroup" aria-label="下一步">
            <button
              v-for="transition in data.transitions" :key="transition.status" type="button"
              class="step" role="radio" :aria-checked="step.target === transition.status"
              :class="{ on: step.target === transition.status }"
              :disabled="saving || !allowed(transition)"
              :title="allowed(transition) ? transition.detail : '该操作需要核查负责人角色'"
              @click="step.target = transition.status"
            >
              {{ verb(transition.status) }}
              <span v-if="!allowed(transition)" class="muted small">（需核查负责人）</span>
            </button>
          </div>
          <p v-else class="muted small">
            已归档是终态，不再有下一步。需要重新处理时，请在风险事项库里新建一条专项核查。
          </p>

          <div v-if="picked" class="step-form">
            <template v-if="needs.reviewer">
              <label class="field-label">上报给</label>
              <Select v-if="REVIEWER_OPTIONS.length" v-model="step.owner" label="上报对象"
                      :options="REVIEWER_OPTIONS" />
              <p v-else class="muted small">没有可上报的核查负责人账号，请管理员先开通。</p>
            </template>
            <template v-else-if="needs.owner">
              <label class="field-label" for="owner">责任人</label>
              <input id="owner" v-model="step.owner" type="text" placeholder="责任人" />
            </template>
            <template v-if="needs.due">
              <label class="field-label" for="due">{{ DUE_LABELS[step.target] }}</label>
              <input id="due" v-model="step.due_at" type="date" :aria-label="DUE_LABELS[step.target]" />
            </template>
            <label class="field-label" for="note">说明</label>
            <textarea id="note" v-model="step.note" rows="2" :placeholder="NOTE_HINTS[step.target]" />
            <label class="field-label">对外口径（可选）</label>
            <Select v-model="step.chosen" label="对外口径" :options="ACTION_OPTIONS" />
            <button type="button" class="move primary" :disabled="saving || !!stepProblem" @click="go">
              {{ verb(step.target) }}
            </button>
            <p v-if="stepProblem" class="muted small">{{ stepProblem }}</p>
          </div>
        </div>

        <p v-if="saved" class="muted small" role="status">{{ saved }}</p>
        <p v-if="decideError" class="error small" role="alert">{{ decideError }}</p>
      </aside>
    </div>
  </AsyncSection>
</template>

<style scoped>
.summary-card { margin-bottom: var(--sp-3); }
.subject { font-size: var(--fs-result); margin: 0 0 var(--sp-2); line-height: var(--lh-tight); }
.facts { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap;
  margin-bottom: var(--sp-2); }
.state { white-space: nowrap; }
.why { margin: 0 0 var(--sp-1); max-width: var(--measure); }
.override { border-color: var(--series-1); color: var(--series-1); }
.fp { border-color: var(--text-muted); color: var(--text-muted); }
.returned { margin-bottom: var(--sp-3); }

/* 左依据、右操作。右栏跟着滚动停在视口里：读到第三段时，写结论的地方仍在手边。
   窄屏时右栏排到下方——手机上没有「旁边」。 */
.workbench { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: var(--sp-4);
  align-items: start; }
.panel { position: sticky; top: var(--sp-3); max-height: calc(100vh - 2 * var(--sp-3));
  overflow-y: auto; scroll-margin-top: var(--sp-3); }
@media (max-width: 1080px) {
  .workbench { grid-template-columns: minmax(0, 1fr); }
  .panel { position: static; max-height: none; }
}
@media print { .panel { display: none; } .workbench { display: block; } }

.block { padding: var(--sp-3) 0; border-top: 1px solid var(--gridline); }
.block:first-of-type { border-top: none; padding-top: 0; }
.block-title { font-size: var(--fs-body); margin: 0 0 var(--sp-2); }
.mini { display: grid; grid-template-columns: auto 1fr; gap: 4px var(--sp-2); margin: 0;
  font-size: var(--fs-aux); }
.mini dt { color: var(--text-muted); }
.mini dd { margin: 0; }
.basis { margin-top: var(--sp-2); font-size: var(--fs-aux); }
.basis summary { cursor: pointer; color: var(--text-secondary); }
.field-label { display: block; margin: var(--sp-2) 0 4px; font-size: var(--fs-note);
  color: var(--text-muted); }
.panel textarea, .panel input { width: 100%; box-sizing: border-box; }
.row { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-2); }
.steps { display: flex; flex-direction: column; gap: 6px; }
.step { text-align: left; }
.step.on { border-color: var(--series-1); box-shadow: inset 2px 0 0 var(--series-1);
  color: var(--text-primary); font-weight: 600; }
.step-form { margin-top: var(--sp-2); padding: var(--sp-2); border-radius: var(--radius);
  background: var(--surface-0); }
.step-form .primary { margin-top: var(--sp-3); width: 100%; }
.claim-me { width: 100%; margin-bottom: var(--sp-1); }
.error { color: var(--status-critical); }

/* 输入框和按钮此前是同一个白底圆角框，分不出哪个能点。动作按钮是主色描边加主色字，
   文本框是内嵌底色——一组是「填」，一组是「按」。 */
.move { text-align: center; color: var(--series-1); border-color: var(--series-1);
  font-weight: 600; }
.move:hover:not(:disabled) { background: var(--series-1); color: var(--surface-1);
  border-color: var(--series-1); }
.move.quiet { color: var(--text-secondary); border-color: var(--border); font-weight: 500; }
.move.primary { background: var(--series-1); color: var(--surface-1); }
.move:disabled, .step:disabled { opacity: 0.55; cursor: not-allowed; }

.anchors { display: flex; gap: var(--sp-3); flex-wrap: wrap; margin-bottom: var(--sp-3);
  font-size: var(--fs-aux); }
.section { scroll-margin-top: var(--sp-6); }
.sub { font-size: var(--fs-title); margin: var(--sp-4) 0 2px; }
.section + .sub { margin-top: var(--sp-2); }
.sources-note { margin-top: var(--sp-4); }

.check { padding: var(--sp-3) 0; border-top: 1px solid var(--gridline); }
.check-head { display: flex; gap: var(--sp-2); align-items: baseline; }
.claim { color: var(--text-primary); }
.reason { margin: var(--sp-1) 0; max-width: var(--measure); }

.group { margin-top: var(--sp-2); }
.sources, .similar { margin: var(--sp-2) 0 0; padding-left: 18px; }
.sources li, .similar li { margin-bottom: var(--sp-1); }
.basis-line { display: block; color: var(--text-muted); }

.spread, .history { margin: var(--sp-2) 0 0; padding-left: 18px; }
.spread li, .history li { margin-bottom: var(--sp-2); display: flex; gap: var(--sp-2);
  align-items: baseline; flex-wrap: wrap; }
.when { color: var(--text-muted); }
.cross-links { display: flex; gap: var(--sp-4); align-items: baseline; flex-wrap: wrap;
  margin-top: var(--sp-3); }
.cross { display: inline-block; margin-top: var(--sp-3); font-weight: 600; }
.cross-links .cross { margin-top: 0; }
/* 逾期用文字说出来，不只用颜色。 */
.late { color: var(--status-serious); font-weight: 600; }
</style>
