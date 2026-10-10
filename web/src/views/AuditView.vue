<script setup>
/**
 * 审计日志 — seven kinds of record in one read-only place.
 *
 * Migrated onto the shared layer, and given the tab the dispatch work earned:
 * what was pushed, to whom, and what was held back. This page is already the
 * one that answers 「跑过什么、谁判过什么」, and an outbound delivery belongs in
 * the same account.
 *
 * 第七类「平台操作」与前六类不同：登录、导出、权限变更都不改变任何业务数据，
 * 没有别的表会顺带记下它们，所以 platform_actions 是唯一一张**为了留痕而建**的表。
 * 动作名与结果名跟着接口一起下来，这一页不自己抄一份。
 *
 * 时间列一律用 `formatDateTime`，不是 `formatDate`。这一页的全部意义是「谁在什么
 * 时候做了什么」，而截到日的话，同一天的几行读起来一模一样——既分不开也排不出
 * 先后。别的页面继续截到日：一条事件的发布日、一次披露的变更日，时分是噪音。
 *
 * The page writes nothing, deliberately. An audit log that can be edited is not
 * an audit log.
 */
import { computed, ref, watch } from 'vue'

import {
  DELIVERY_REASON_LABELS, DELIVERY_STATUS_META, DISPOSITION_STATUS_META, JUDGEMENT_LABELS, SUPPRESSED_REASON_LABELS, TASK_KIND_LABELS, VERDICT_META, api, companyLabel, formatDate, formatDateTime, labelText,
} from '../api.js'
import { ROLE_LABELS } from '../session.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'

const tab = ref('runs')
const data = ref(null)
const loading = ref(true)
const error = ref('')
const result = ref('')

// 「模型与提示词」页签已移除（I-06）：哪个模型、哪一版提示词只留在服务器的技术日志里。
const TABS = {
  runs: '处理运行', decisions: '人工判定',
  dispositions: '预警处置', actions: '平台操作', deliveries: '推送台账',
  events: '采集审计',
}

async function load() {
  loading.value = true
  error.value = ''
  const previous = data.value
  try {
    if (tab.value === 'runs') data.value = await api.auditRuns()
    else if (tab.value === 'decisions') data.value = await api.auditDecisions()
    else if (tab.value === 'dispositions') data.value = await api.notifyDispositionLog({ limit: 100 })
    else if (tab.value === 'actions') data.value = await api.auditActions({ limit: 100 })
    else if (tab.value === 'deliveries') data.value = await api.notifyOutbox({ limit: 100 })
    else data.value = await api.auditEvents({ result: result.value || undefined })
  } catch (exception) {
    error.value = exception.message
    data.value = previous
  } finally {
    loading.value = false
  }
}

const seconds = (row) => (row.completed_at && row.started_at
  ? `${Math.round((new Date(row.completed_at) - new Date(row.started_at)) / 1000)} 秒` : '—')

watch([tab, result], load)
load()

const resultOptions = computed(() => (data.value?.results || [])
  .map((row) => ({ value: row.value, label: row.label, count: row.count })))

const RUN_COLUMNS = [
  { key: 'started_at', title: '开始', slot: 'started', nowrap: true, width: '150px' },
  { key: 'elapsed', title: '用时', slot: 'elapsed', nowrap: true, width: '80px' },
  { key: 'status', title: '状态', slot: 'status', nowrap: true, width: '120px' },
  { key: 'reference', title: '批次编号', slot: 'reference', nowrap: true, width: '100px' },
  { key: 'documents_seen', title: '看到', align: 'right', nowrap: true, width: '70px' },
  { key: 'documents_processed', title: '处理', align: 'right', nowrap: true, width: '70px' },
  { key: 'analysed_documents', title: '智能研判', align: 'right', nowrap: true, width: '80px' },
  { key: 'error', title: '失败原因', slot: 'error' },
]
const DECISION_COLUMNS = [
  { key: 'decided_at', title: '时间', slot: 'decided', nowrap: true, width: '150px' },
  { key: 'reviewer', title: '复核人', slot: 'reviewer', nowrap: true, width: '130px' },
  { key: 'kind', title: '类型', slot: 'kind', nowrap: true, width: '150px' },
  { key: 'action', title: '动作', slot: 'action', nowrap: true, width: '70px' },
  { key: 'system_verdict', title: '系统当时的结论', slot: 'system', width: '150px' },
  { key: 'judgement', title: '人的结论', slot: 'human' },
  { key: 'note', title: '说明', slot: 'note' },
]
const DISPOSITION_COLUMNS = [
  { key: 'decided_at', title: '时间', slot: 'decided', nowrap: true, width: '150px' },
  { key: 'actor', title: '处置人', slot: 'actor', nowrap: true, width: '130px' },
  { key: 'status', title: '状态', slot: 'status', nowrap: true, width: '96px' },
  { key: 'recommended', title: '系统建议', slot: 'recommended', nowrap: true, width: '110px' },
  { key: 'chosen', title: '人的选择', slot: 'chosen', nowrap: true, width: '150px' },
  { key: 'note', title: '说明', slot: 'note' },
]

const DELIVERY_COLUMNS = [
  { key: 'queued_at', title: '入队', slot: 'queued', nowrap: true, width: '150px' },
  { key: 'status', title: '状态', slot: 'status', nowrap: true, width: '100px' },
  { key: 'reason', title: '触发', slot: 'reason', nowrap: true, width: '110px' },
  { key: 'channel', title: '渠道', slot: 'channel', nowrap: true, width: '84px' },
  { key: 'alert_id', title: '预警', slot: 'alert' },
  { key: 'http_status', title: 'HTTP', align: 'right', nowrap: true, width: '70px' },
]
/** 「对象」一列刻意不写死宽度：账号名短，报告标题长，同一列里两种都要放得下。 */
const ACTION_COLUMNS = [
  { key: 'occurred_at', title: '时间', slot: 'at', nowrap: true, width: '150px' },
  { key: 'actor', title: '操作者', slot: 'actor', nowrap: true, width: '150px' },
  { key: 'action', title: '动作', slot: 'action', nowrap: true, width: '130px' },
  { key: 'outcome', title: '结果', slot: 'outcome', nowrap: true, width: '90px' },
  { key: 'object_label', title: '对象', slot: 'object' },
  { key: 'client', title: '来源', slot: 'client', nowrap: true, width: '130px' },
  { key: 'detail', title: '说明', slot: 'detail' },
]

const CHANNEL_NAMES = { inapp: '站内通知', webhook: '外部推送', email: '邮件' }

const EVENT_COLUMNS = [
  { key: 'at', title: '时间', slot: 'at', nowrap: true, width: '150px' },
  { key: 'source_name', title: '来源', slot: 'source', nowrap: true, width: '180px' },
  { key: 'action', title: '操作', nowrap: true, width: '130px' },
  { key: 'result_label', title: '结果', slot: 'result', nowrap: true, width: '80px' },
  { key: 'detail', title: '说明', slot: 'detail' },
]
</script>

<template>
  <PageHeader title="审计日志"
      note="集中记录处理任务、人工判定、推送投递与平台操作等七类留痕。">
  </PageHeader>

  <FilterBar :active="result ? 1 : 0" @reset="result = ''">
    <button v-for="(label, key) in TABS" :key="key" type="button"
            :class="{ on: tab === key }" @click="tab = key">
      {{ label }}
    </button>
    <EnumSelect v-if="tab === 'events'" v-model="result"
                :options="resultOptions" all-label="全部结果" />
  </FilterBar>

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <div v-if="tab === 'runs'" class="card">
      <DataTable :columns="RUN_COLUMNS" :rows="data.items" row-key="reference">
        <template #started="{ row }"><span class="mono small">{{ formatDateTime(row.started_at) }}</span></template>
        <template #elapsed="{ row }"><span class="small">{{ seconds(row) }}</span></template>
        <template #status="{ row }">
          <span class="small">{{ row.status_label }}</span>
        </template>
        <template #reference="{ row }"><span class="mono small">{{ row.reference }}</span></template>
        <!-- 只给失败类别；原始报错留在服务器的技术日志里，凭批次编号检索（I-06）。 -->
        <template #error="{ row }">
          <span class="muted small">{{ row.error?.label || '—' }}</span>
        </template>
      </DataTable>
    </div>

    <div v-else-if="tab === 'decisions'" class="card">
      <p v-if="!data.items.length" class="muted small">
        暂无人工判定记录。准确率尚未测量，不代表为 0；复核队列中有 300 条待判。
      </p>
      <DataTable v-else :columns="DECISION_COLUMNS" :rows="data.items" row-key="decision_id">
        <template #decided="{ row }"><span class="mono small">{{ formatDateTime(row.decided_at) }}</span></template>
        <template #reviewer="{ row }"><span class="small">{{ row.reviewer }}（{{ ROLE_LABELS[row.role] || row.role }}）</span></template>
        <template #kind="{ row }">
          <span class="small">
            {{ TASK_KIND_LABELS[row.kind] || row.kind }} · {{ companyLabel(row.company_id) }}
          </span>
        </template>
        <template #action="{ row }">
          <span class="small">
            {{ { judge: '判定', approve: '通过', reject: '退回' }[row.action] || row.action }}
          </span>
        </template>
        <template #system="{ row }"><span class="small">{{ VERDICT_META[row.system_verdict]?.label || row.system_verdict || '—' }}</span></template>
        <template #human="{ row }">
          <span class="small">
            {{ JUDGEMENT_LABELS[row.judgement] || row.judgement || '—' }}
            <template v-if="row.correct_label">→ {{ labelText(row.kind, row.correct_label) }}</template>
            <template v-if="row.evidence_ok !== null && row.evidence_ok !== undefined">
              · 证据{{ row.evidence_ok ? '对' : '错' }}</template>
          </span>
        </template>
        <template #note="{ row }"><span class="muted small">{{ row.note || '—' }}</span></template>
      </DataTable>
    </div>

    <div v-else-if="tab === 'dispositions'" class="card">
      <p v-if="!data.items.length" class="muted small">
        暂无处置记录。可在「风险预警」中展开任一事项，在建议下方记录处置。
      </p>
      <DataTable v-else :columns="DISPOSITION_COLUMNS" :rows="data.items" row-key="alert_id">
        <template #decided="{ row }"><span class="mono small">{{ formatDateTime(row.decided_at) }}</span></template>
        <template #actor="{ row }"><span class="small">{{ row.actor }}（{{ ROLE_LABELS[row.role] || row.role }}）</span></template>
        <template #status="{ row }">
          <span class="badge">
            <span class="pip" :style="{ background: DISPOSITION_STATUS_META[row.status]?.color }"
                  aria-hidden="true" />
            <span class="small">{{ DISPOSITION_STATUS_META[row.status]?.label || row.status }}</span>
          </span>
        </template>
        <template #recommended="{ row }">
          <span class="muted small">{{ row.recommended_label || row.recommended }}</span>
        </template>
        <template #chosen="{ row }">
          <span class="small">{{ row.chosen_label || row.chosen }}</span>
          <span v-if="row.diverged" class="tag divergent">不同</span>
        </template>
        <template #note="{ row }"><span class="muted small">{{ row.note || '—' }}</span></template>
      </DataTable>
    </div>

    <div v-else-if="tab === 'actions'" class="card">
      <p v-if="!data.items.length" class="muted small">
        暂无平台操作留痕。该记录自启用之日起生效，不作回填；
        启用前发生的登录与导出操作未被记录。
      </p>
      <template v-else>
        <p class="muted small">
          共 {{ data.total.toLocaleString() }} 行：
          <span v-for="row in data.by_action" :key="row.action">
            {{ data.actions[row.action] || row.action }} {{ row.rows }}<template v-if="row !== data.by_action[data.by_action.length - 1]">、</template>
          </span>
        </p>
        <DataTable :columns="ACTION_COLUMNS" :rows="data.items" row-key="action_seq">
          <template #at="{ row }"><span class="mono small">{{ formatDateTime(row.occurred_at) }}</span></template>
          <template #actor="{ row }">
            <span class="small">{{ row.actor }}<template v-if="row.role">（{{ ROLE_LABELS[row.role] || row.role }}）</template></span>
          </template>
          <template #action="{ row }">
            <span class="small">{{ data.actions[row.action] || row.action }}</span>
          </template>
          <template #outcome="{ row }">
            <span class="small" :class="{ refused: row.outcome !== 'ok' }">
              {{ data.outcomes[row.outcome] || row.outcome }}
            </span>
          </template>
          <template #object="{ row }">
            <span class="small">
              <template v-if="row.object_kind_label || row.object_label">
                {{ row.object_kind_label || '' }}<template v-if="row.object_label">「{{ row.object_label }}」</template>
              </template>
              <template v-else>—</template>
            </span>
          </template>
          <template #client="{ row }"><span class="mono small">{{ row.client || '—' }}</span></template>
          <template #detail="{ row }"><span class="muted small">{{ row.detail || '—' }}</span></template>
        </DataTable>
      </template>
    </div>

    <div v-else-if="tab === 'deliveries'" class="card">
      <p v-if="!data.items.length" class="muted small">
        暂无投递记录。请先在「订阅与推送规则」中新建规则，再在「推送记录」中执行评估。
      </p>
      <DataTable v-else :columns="DELIVERY_COLUMNS" :rows="data.items" row-key="delivery_id">
        <template #queued="{ row }"><span class="mono small">{{ formatDateTime(row.queued_at) }}</span></template>
        <template #status="{ row }">
          <span class="badge">
            <span class="pip" :style="{ background: DELIVERY_STATUS_META[row.status]?.color }"
                  aria-hidden="true" />
            <span class="small">{{ DELIVERY_STATUS_META[row.status]?.label || row.status }}</span>
          </span>
        </template>
        <template #reason="{ row }">
          <span class="small">{{ DELIVERY_REASON_LABELS[row.reason] || row.reason }}</span>
        </template>
        <template #channel="{ row }"><span class="small">{{ CHANNEL_NAMES[row.channel] || '其他' }}</span></template>
        <template #alert="{ row }">
          <div class="small">{{ (row.payload?.summary || '（无摘要）').slice(0, 48) }}</div>
          <div v-if="row.suppressed_reason" class="muted small">
            {{ SUPPRESSED_REASON_LABELS[row.suppressed_reason] || row.suppressed_reason }}
          </div>
        </template>
      </DataTable>
    </div>

    <div v-else class="card">
      <p class="muted small">
        读取日志末尾 {{ data.scanned.toLocaleString() }} 行{{ data.truncated ? '（更早的内容未读入）' : '' }}，
        最新在前。
      </p>
      <DataTable :columns="EVENT_COLUMNS" :rows="data.items" :row-key="(row, index) => index">
        <template #at="{ row }"><span class="mono small">{{ formatDateTime(row.at) }}</span></template>
        <template #source="{ row }"><span class="small">{{ row.source_name || '—' }}</span></template>
        <template #result="{ row }">
          <span class="small" :class="{ refused: row.result === 'failure' }">{{ row.result_label }}</span>
        </template>
        <!-- 不再整行输出原始日志（带着本地路径、哈希与异常原文，I-06）。 -->
        <template #detail="{ row }">
          <span class="muted small detail">
            <template v-if="row.error">{{ row.error.label }}<template v-if="row.http_status">（HTTP {{ row.http_status }}）</template></template>
            <template v-else-if="row.title">{{ row.title }}</template>
            <template v-else>—</template>
          </span>
        </template>
      </DataTable>
    </div>
  </AsyncSection>
</template>

<style scoped>
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 10px; }
section.card, div.card { margin-bottom: 12px; }
button.on { border-color: var(--status-good); }
.plain { list-style: none; margin: 6px 0 0; padding: 0; }
.detail { display: inline-block; max-width: 520px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 两行封顶。整段留在 title 属性里，需要读全的人悬停即可，不必让它挤掉整页。 */
.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  max-width: 640px;
}
.badge { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.pip { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.tag.divergent { color: var(--status-serious); border-color: var(--status-serious); margin-left: 6px; }
/* 被拒绝与失败都标出来：一页全是「成功」的审计记录，读者会跳着看。 */
.refused { color: var(--status-serious); }
</style>
