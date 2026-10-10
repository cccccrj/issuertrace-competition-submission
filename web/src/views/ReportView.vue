<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  CREDIBILITY_LABELS, ISSUE_STATUS_LABELS, METHOD_LABELS, VERDICT_META, api, basisLabel, companyLabel, eventTypeLabel, formatDate, targetText, impactObjectLabel, impactText, issueLabel, levelLabel, tierLabel
} from '../api.js'
import Loading from '../components/ui/Loading.vue'

const route = useRoute()
const payload = ref(null)
const error = ref(null)

async function load() {
  payload.value = null
  error.value = null
  try {
    payload.value = route.params.kind === 'issues'
      ? await api.issue(route.params.id)
      : await api.event(route.params.id)
  } catch (exception) {
    error.value = exception.message
  }
}

watch(() => [route.params.kind, route.params.id], load, { immediate: true })

const isEvent = computed(() => route.params.kind !== 'issues')
const generatedAt = new Date().toISOString().slice(0, 16).replace('T', ' ')
const print = () => window.print()
const cited = computed(() => (payload.value?.claims || []).filter((claim) => claim.evidence_page))
</script>

<template>
  <div class="report">
    <div v-if="error" class="empty">{{ error }}</div>
    <Loading v-else-if="!payload" />

    <template v-else-if="isEvent">
      <!-- Printed as-is: everything on this page comes from the stored tables,
           and a section with no data says so rather than being written around. -->
      <header class="masthead">
        <div class="org">上市公司事件智能平台 · 事件报告</div>
        <h1>{{ payload.summary }}</h1>
        <div class="meta">
          {{ companyLabel(payload.company_id) }} ·
          {{ formatDate(payload.time_basis === 'event_time' ? payload.event_time : payload.published_at) }}（{{ basisLabel(payload.time_basis) }}）·
          <template v-if="payload.target_time">{{ targetText(payload.target_time) }} ·</template>
          来源 {{ payload.source_name || '—' }}
        </div>
      </header>

      <section>
        <h2>一、摘要</h2>
        <p>{{ payload.summary }}</p>
        <p class="meta">
          事件类型：{{ payload.event_types.map(eventTypeLabel).join('、') || '—' }}；
          专题：{{ payload.issue_codes.map(issueLabel).join('、') || '—' }}；
          影响方向：{{ impactText(payload.impact_direction, payload.impact_magnitude) }}
        </p>
      </section>

      <section>
        <h2>二、事件主体与影响对象</h2>
        <p>主体：{{ payload.event_subjects.join('、') || '—' }}</p>
        <p>影响对象：{{ payload.affected_objects.map(impactObjectLabel).join('、') || '—' }}</p>
        <p v-if="payload.amounts.length">涉及金额：{{ payload.amounts.join('、') }}</p>
      </section>

      <section>
        <h2>三、可信度</h2>
        <template v-if="payload.alerts.length">
          <p>
            窗口可信度 {{ payload.alerts[0].credibility.toFixed(2) }}
            （{{ CREDIBILITY_LABELS[payload.alerts[0].credibility_label] }}）：
            {{ payload.alerts[0].credibility_basis.verdict_note }}；
            来源等级 {{ tierLabel(payload.alerts[0].credibility_basis.source_tier) }}；
            窗口内独立信源 {{ payload.alerts[0].credibility_basis.independent_components }} 条。
          </p>
        </template>
        <p v-else>
          该事件未触发预警，无窗口级可信度评分。系统抽取置信度
          {{ payload.extraction_confidence?.toFixed?.(2) ?? '—' }}，为系统自评值，不代表人工核验结论。
        </p>
      </section>

      <section>
        <h2>四、证据与裁决</h2>
        <p v-if="!payload.claims.length">该事件未能拆出可定位的原子主张，因此没有可核验的裁决。</p>
        <table v-else>
          <thead>
            <tr><th>原子主张</th><th>裁决</th><th>证据出处</th></tr>
          </thead>
          <tbody>
            <tr v-for="claim in payload.claims" :key="claim.claim_id">
              <td>{{ claim.text }}</td>
              <td class="nowrap">
                {{ VERDICT_META[claim.verdict]?.label || '待裁决' }}
                <div class="meta">{{ METHOD_LABELS[claim.verdict_method] || claim.verdict_method || '—' }}</div>
              </td>
              <td class="nowrap">
                <template v-if="claim.evidence_page">
                  第 {{ claim.evidence_page }} 页<div class="meta">{{ claim.evidence_section }}</div>
                </template>
                <span v-else>—</span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="payload.claims.length" class="meta">
          共 {{ payload.claims.length }} 条主张，其中 {{ cited.length }} 条给出了可定位页码。
        </p>
      </section>

      <section>
        <h2>五、时间线与阶段风险</h2>
        <template v-if="payload.timeline">
          <p>
            {{ formatDate(payload.timeline.timeline_at) }}（{{ basisLabel(payload.timeline.time_basis) }}），
            处于{{ payload.timeline.phase_label }}阶段。风险分
            {{ payload.timeline.base_risk_score.toFixed(1) }} ×
            {{ payload.timeline.phase_coefficient }} =
            {{ payload.timeline.adjusted_risk_score.toFixed(1) }}
            （{{ levelLabel(payload.timeline.adjusted_risk_level) }}）。
          </p>
          <p class="meta">
            该事件包含 {{ payload.timeline.new_fact_count }} 条同专题此前未出现的新增事实，
            共 {{ payload.timeline.claim_count }} 条主张。
          </p>
        </template>
        <p v-else>无时间线记录。</p>
      </section>

      <section>
        <h2>六、传播情况</h2>
        <p v-if="!payload.propagation.length">未发现该报道被复制传播的记录。</p>
        <p v-for="chain in payload.propagation" v-else :key="chain.component_id">
          所在传播链共 {{ chain.member_count }} 个版本、{{ chain.source_count }} 家发布方，
          时间跨度 {{ formatDate(chain.first_published_at) }} 至 {{ formatDate(chain.last_published_at) }}。
        </p>
      </section>

      <section>
        <h2>七、预警理由</h2>
        <p v-if="!payload.alerts.length">该事件未触发任何预警规则。</p>
        <ul v-else>
          <li v-for="alert in payload.alerts" :key="alert.alert_id">
            {{ alert.headline }}（{{ levelLabel(alert.risk_level) }}，修正后
            {{ alert.adjusted_risk_score.toFixed(1) }} 分）：
            {{ alert.reasons.map((reason) => reason.label).join('；') }}
          </li>
        </ul>
      </section>
    </template>

    <template v-else>
      <header class="masthead">
        <div class="org">上市公司事件智能平台 · 专题报告</div>
        <h1>{{ issueLabel(payload.issue_code) }}</h1>
        <div class="meta">
          {{ companyLabel(payload.company_id) }} · 状态 {{ ISSUE_STATUS_LABELS[payload.status] || payload.status }} ·
          {{ payload.event_count }} 个事件 · 独立信源 {{ payload.independent_source_count }}
        </div>
      </header>

      <section>
        <h2>一、专题概况</h2>
        <p>
          首现于 {{ formatDate(payload.first_seen_at) }}，最近一次出现在
          {{ formatDate(payload.last_seen_at) }}，风险分
          {{ payload.risk_score?.toFixed?.(1) ?? '—' }}（{{ levelLabel(payload.risk_level) }}）。
        </p>
      </section>

      <section>
        <h2>二、主张裁决分布</h2>
        <p v-if="!Object.keys(payload.verdicts || {}).length">该专题下尚无已裁决的主张。</p>
        <p v-else>
          <span v-for="(count, verdict) in payload.verdicts" :key="verdict">
            {{ VERDICT_META[verdict]?.label || verdict }} {{ count }} 条；
          </span>
        </p>
      </section>

      <section>
        <h2>三、状态变化</h2>
        <p v-if="!payload.status_history?.length">状态自首次判定以来没有变过。</p>
        <table v-else>
          <thead><tr><th>时间</th><th>由</th><th>至</th><th>事件数</th><th>理由</th></tr></thead>
          <tbody>
            <tr v-for="(row, index) in payload.status_history" :key="index">
              <td class="nowrap">{{ formatDate(row.changed_at) }}</td>
              <td class="nowrap">{{ ISSUE_STATUS_LABELS[row.previous_status] || row.previous_status || '首次判定' }}</td>
              <td class="nowrap">{{ ISSUE_STATUS_LABELS[row.status] || row.status }}</td>
              <td class="nowrap mono">{{ row.previous_event_count }} → {{ row.recent_event_count }}</td>
              <td>{{ row.reason }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section>
        <h2>四、时间线</h2>
        <table>
          <thead><tr><th>时间</th><th>口径</th><th>事件</th></tr></thead>
          <tbody>
            <tr v-for="entry in payload.timeline" :key="entry.event_id">
              <td class="nowrap">{{ formatDate(entry.sort_time) }}</td>
              <td class="nowrap">{{ basisLabel(entry.time_basis) }}</td>
              <td>{{ entry.summary }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>

    <footer v-if="payload" class="colophon">
      本报告由平台按已存储的处理结果生成于 {{ generatedAt }}（UTC），未做任何再计算或补写；
      所有页码均指向采集到的原始披露文件。
      <button class="print" @click="print">打印 / 导出 PDF</button>
      <a class="print" :href="`/api/reports/${route.params.kind === 'issues' ? 'issues' : 'events'}/${route.params.id}.docx`">
        导出 Word（按七项要素成文）
      </a>
    </footer>
  </div>
</template>

<style scoped>
.report { max-width: 780px; margin: 0 auto; padding: 40px 32px 64px; color: var(--text-primary); }
.masthead { border-bottom: 1px solid var(--baseline); padding-bottom: 14px; margin-bottom: 22px; }
.org { font-size: var(--fs-note); letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-muted); }
h1 { font-size: 21px; font-weight: 640; letter-spacing: -0.02em; margin: 8px 0 6px; line-height: 1.35; }
h2 { font-size: var(--fs-body); font-weight: 620; margin: 24px 0 8px; }
p { font-size: var(--fs-body); line-height: 1.8; margin: 0 0 6px; }
.meta { font-size: var(--fs-note); color: var(--text-secondary); }
table { width: 100%; border-collapse: collapse; margin-top: 6px; }
th { font-size: var(--fs-note); }
td { font-size: var(--fs-body); }
ul { font-size: var(--fs-body); line-height: 1.8; padding-left: 20px; margin: 0; }
.colophon { margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--gridline);
  font-size: var(--fs-note); color: var(--text-muted); display: flex; justify-content: space-between;
  gap: 16px; align-items: flex-end; }

a.print { display: inline-block; margin-left: 8px; text-decoration: none; }

@media print {
  /* The printed page is the same document, minus the control that printed it. */
  .print { display: none; }
  .report { max-width: none; padding: 0; }
  section { break-inside: avoid; }
}
</style>
