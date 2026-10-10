<script setup>
/**
 * 上报与推送记录。上半页是**内部上报与退回**——人把一条事项交给上级、上级退回来，
 * 取自处置流水；下半页是对外推送的投递记录。此前这一页只有后者，侧栏却叫它「上报与
 * 推送记录」：点进来找「我上周报给谁了」的人找不到（修改思路 v2 §十五）。
 *
 * The outbox: what was sent, what is waiting, and — the part that matters most
 * — what was deliberately not sent and why.
 *
 * A notification feature that only shows successes is one nobody can trust,
 * because the interesting question is never "did that arrive" but "was there
 * something I should have been told about that I wasn't". Suppressed rows carry
 * the rule that stopped them, so a merge is legible as a policy rather than
 * indistinguishable from a bug.
 */
import { computed, onMounted, ref, watch } from 'vue'

import {
  CHANNEL_LABELS, DELIVERY_REASON_LABELS, DELIVERY_STATUS_META, SUPPRESSED_REASON_LABELS, VERDICT_META, api, companyLabel, formatDate, issueLabel, IMPACT_OBJECT_LABELS, CREDIBILITY_LABELS
} from '../api.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import Pager from '../components/Pager.vue'
import StatTile from '../components/StatTile.vue'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const expanded = ref(null)
const offset = ref(0)
const filters = ref({ status: '', channel: '', reason: '' })
const busy = ref('')
const notice = ref('')

const LIMIT = 25

/* 内部上报与退回：只取「走进」这两个状态的那几步，最近的 20 条。 */
const handoffs = ref([])
async function loadHandoffs() {
  try {
    handoffs.value = (await api.auditDispositions({ to_status: ['reporting', 'returned'], limit: 20 })).items
  } catch {
    handoffs.value = []
  }
}
onMounted(loadHandoffs)

const STATUS_LABELS = Object.fromEntries(
  Object.entries(DELIVERY_STATUS_META).map(([key, meta]) => [key, meta.label]))

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.notifyOutbox({ ...filters.value, limit: LIMIT, offset: offset.value })
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(filters, () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)

const active = computed(() => Object.values(filters.value).filter(Boolean).length)
function reset() { filters.value = { status: '', channel: '', reason: '' } }

const counts = computed(() => Object.fromEntries(
  (data.value?.by_status || []).map((row) => [row.value, row.count])))

const suppressed = computed(() => data.value?.suppressed_reasons || [])

async function run(action, label) {
  busy.value = label
  notice.value = ''
  try {
    const result = await (action === 'evaluate' ? api.notifyEvaluate() : api.notifyFlush({ limit: 50 }))
    notice.value = action === 'evaluate'
      ? `评估完成：入队 ${result.queued} 条，抑制 ${result.suppressed} 条，未变化 ${result.unchanged} 条。`
      : `尝试 ${result.attempted} 条：${Object.entries(result.by_status)
          .map(([key, count]) => `${STATUS_LABELS[key] || key} ${count}`).join('，')}。`
    await load()
  } catch (exception) {
    error.value = exception.message
  } finally {
    busy.value = ''
  }
}

const COLUMNS = [
  { key: 'status', title: '状态', slot: 'status', nowrap: true, width: '108px' },
  { key: 'reason', title: '触发原因', slot: 'reason', nowrap: true, width: '132px' },
  { key: 'subject', title: '预警', slot: 'subject' },
  { key: 'channel', title: '渠道', slot: 'channel', nowrap: true, width: '92px' },
  { key: 'queued_at', title: '入队时间', slot: 'time', nowrap: true, width: '150px' },
]
</script>

<template>
  <PageHeader title="上报与推送记录"
      note="记录风险事项的内部上报、外部预警推送及发送状态。">
    <template #actions>
      <button type="button" :disabled="!!busy" @click="run('evaluate', '评估')">
        {{ busy === '评估' ? '评估中…' : '重新评估' }}
      </button>
      <button type="button" :disabled="!!busy" @click="run('flush', '发送')">
        {{ busy === '发送' ? '处理中…' : '处理队列' }}
      </button>
    </template>
  </PageHeader>

  <Callout v-if="notice" tone="ok">{{ notice }}</Callout>

  <section class="card handoffs">
    <h2>内部上报与退回</h2>
    <p class="muted small">记录事项的移交人、接收人及移交原因。完整处置过程见工作台「处置与记录」及审计日志。</p>
    <ol v-if="handoffs.length" class="handoff-list">
      <li v-for="row in handoffs" :key="row.event_seq">
        <span class="mono muted">{{ formatDate(row.occurred_at) }}</span>
        <span class="tag" :class="row.to_status">{{ row.to_status === 'returned' ? '退回' : '上报' }}</span>
        <b>{{ row.actor }}</b> → <b>{{ row.owner || '—' }}</b>
        <RouterLink :to="`/risks/${row.alert_id}`">{{ row.headline || '（事项已被重算替换）' }}</RouterLink>
        <span v-if="row.conclusion_label" class="muted small">结论：{{ row.conclusion_label }}</span>
        <span v-if="row.note" class="muted small">「{{ row.note }}」</span>
      </li>
    </ol>
    <p v-else class="muted small">暂无内部上报。在风险研判工作台执行「上报复核」后，记录将在此显示。</p>
  </section>

  <h2 class="part">外部推送</h2>

  <div class="tiles">
    <StatTile label="已送达" :value="counts.sent || 0" />
    <StatTile label="演示模式" :value="counts.dry_run || 0" note="已渲染报文，未发送" />
    <StatTile label="待发送" :value="counts.queued || 0" />
    <StatTile label="发送失败" :value="counts.failed || 0" />
    <StatTile label="被抑制" :value="counts.suppressed || 0" note="规则拦下，见下方原因" />
  </div>

  <section v-if="suppressed.length" class="card reasons">
    <h2>被抑制的推送，按原因</h2>
    <ul>
      <li v-for="row in suppressed" :key="row.value">
        <b class="mono">{{ row.count }}</b>
        {{ SUPPRESSED_REASON_LABELS[row.value] || row.value }}
      </li>
    </ul>
  </section>

  <FilterBar :active="active" @reset="reset">
    <EnumSelect v-model="filters.status" :labels="STATUS_LABELS" all-label="全部状态" />
    <EnumSelect v-model="filters.channel" :labels="CHANNEL_LABELS" all-label="全部渠道" />
    <EnumSelect v-model="filters.reason" :labels="DELIVERY_REASON_LABELS" all-label="全部触发原因" />
  </FilterBar>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    empty-text="暂无投递记录。点击「重新评估」后，系统将按规则生成待发送内容并入队，此操作不会实际发送。"
  >
    <div class="card">
      <DataTable
        :columns="COLUMNS"
        :rows="data.items"
        row-key="delivery_id"
        expandable
        v-model:expanded="expanded"
      >
        <template #status="{ row }">
          <span class="badge">
            <span class="pip" :style="{ background: DELIVERY_STATUS_META[row.status]?.color }" aria-hidden="true" />
            {{ DELIVERY_STATUS_META[row.status]?.label || row.status }}
          </span>
        </template>
        <template #reason="{ row }">{{ DELIVERY_REASON_LABELS[row.reason] || row.reason }}</template>
        <template #subject="{ row }">
          <div>{{ row.payload.summary }}</div>
          <div class="muted small">
            {{ companyLabel(row.company_id) }} · {{ issueLabel(row.issue_code) }}
            <template v-if="row.suppressed_reason">
              · <b>{{ SUPPRESSED_REASON_LABELS[row.suppressed_reason] || row.suppressed_reason }}</b>
            </template>
          </div>
        </template>
        <template #channel="{ row }">{{ CHANNEL_LABELS[row.channel] || row.channel }}</template>
        <template #time="{ row }">{{ formatDate(row.queued_at) }}</template>

        <template #expand="{ row }">
          <div class="detail">
            <div>
              <h3>报文（将要发出的内容）</h3>
              <dl class="detail-grid">
                <dt>摘要</dt><dd>{{ row.payload.summary }}</dd>
                <dt>事件主体</dt><dd>{{ row.payload.subjects?.join('、') || '—' }}</dd>
                <dt>影响对象</dt><dd>{{ (row.payload.affected_object_labels || row.payload.affected_objects?.map((code) => IMPACT_OBJECT_LABELS[code] || code))?.join('、') || '—' }}</dd>
                <dt>可信度</dt><dd>{{ row.payload.credibility_text || CREDIBILITY_LABELS[row.payload.credibility_label] || '—' }}（{{ row.payload.credibility }}）</dd>
                <!-- 理由是 {code, label}；此前被 str() 成了 Python 字典文本直接印出来（I-06）。 -->
                <dt>预警理由</dt><dd>{{ row.payload.reasons?.map((item) => item.label || item).join('、') || '—' }}</dd>
                <dt>窗口</dt><dd>{{ row.payload.window?.start }} — {{ row.payload.window?.end }}</dd>
              </dl>
              <h3>证据来源</h3>
              <ul class="evidence-list">
                <li v-for="(item, index) in row.payload.evidence" :key="index">
                  {{ item.claim }}
                  <span class="muted">［{{ VERDICT_META[item.verdict]?.label || item.verdict }}］{{ item.document }} 第 {{ item.page }} 页</span>
                </li>
                <li v-if="!row.payload.evidence?.length" class="muted">该预警下没有可引用的主张。</li>
              </ul>
            </div>
            <div>
              <h3>投递情况</h3>
              <dl class="detail-grid">
                <!-- 只给推送到了哪个站点、失败属于哪一类；完整地址、对方回包与异常原文
                     只在服务器的投递记录里（I-06）。 -->
                <dt>目标站点</dt><dd class="mono">{{ row.target_host || '—' }}</dd>
                <dt>尝试次数</dt><dd class="mono">{{ row.attempts }}</dd>
                <dt>HTTP</dt><dd class="mono">{{ row.http_status ?? '—' }}</dd>
                <dt v-if="row.error">失败原因</dt><dd v-if="row.error">{{ row.error.label }}</dd>
                <dt>送达时间</dt><dd>{{ row.delivered_at ? formatDate(row.delivered_at) : '—' }}</dd>
              </dl>
            </div>
          </div>
        </template>
      </DataTable>
      <Pager :total="data.total" :limit="LIMIT" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.handoffs { margin-bottom: var(--sp-4); }
.handoffs h2, .part { font-size: var(--fs-body); font-weight: 650; margin: 0 0 6px; }
.part { margin-top: var(--sp-2); }
.handoff-list { margin: var(--sp-2) 0 0; padding-left: 18px; }
.handoff-list li { display: flex; gap: var(--sp-2); align-items: baseline; flex-wrap: wrap; margin-bottom: 6px; }
.tag.returned { color: var(--status-critical); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 12px; margin-bottom: 12px; }
.reasons { margin-bottom: 12px; }
.reasons ul { margin: 8px 0 0; padding-left: 18px; font-size: var(--fs-body); }
.reasons li { margin-bottom: 3px; }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0; }
h3 { font-size: var(--fs-aux); font-weight: 650; margin: 0 0 6px; color: var(--text-secondary); }
.badge { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.pip { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.detail { display: grid; grid-template-columns: 1.6fr 1fr; gap: 20px; padding: 4px 0; }
.evidence-list { margin: 0; padding-left: 18px; font-size: var(--fs-body); }
.evidence-list li { margin-bottom: 4px; }
</style>
