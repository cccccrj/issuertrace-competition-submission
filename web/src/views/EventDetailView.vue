<script setup>
/**
 * 事件详情 — 「答辩演示的核心页面」, and the page the brief's seven required
 * outputs are checked against.
 *
 * The ①–⑦ numbering is a contract, not a layout choice: each mark is one of the
 * seven outputs 命题二 asks for, and `__tests__/views/event-detail.test.js`
 * pins the order. Sections may be added; none may be renumbered or dropped.
 *
 * Phase G is incremental by design. What changed: the elements table moved onto
 * `ui/DataTable`, the credibility breakdown became the same contribution bar
 * the alert page uses (weight × value is what sums to the score; a table of
 * weights and values asks the reader to multiply), and ⑦ gained the
 * recommended handling that `api/dispositions.py` has been deriving all along.
 *
 * Two sentences on this page are pinned by name in the test file. Both draw a
 * distinction the page would otherwise blur — a model's self-reported
 * confidence is not a credibility score, and an event with no locatable claims
 * is excluded from the verification denominator rather than counted as a
 * failure — and both are the kind of copy that gets tidied by someone who has
 * not read why it is there.
 */
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  BASIS_LABELS, CLAIM_TYPE_HINTS, CREDIBILITY_LABELS, CREDIBILITY_PARTS, METHOD_LABELS, PRODUCTION_LABELS, api, basisLabel, claimTypeLabel, companyLabel, eventTypeLabel, formatDate, targetText, impactObjectLabel, impactText, issueLabel, sentimentLabel,
} from '../api.js'
import EChart from '../charts/EChart.vue'
import BasisDetails from '../components/BasisDetails.vue'
import RiskBreakdown from '../components/RiskBreakdown.vue'
import { partBar } from '../charts/options/index.js'
import CausalEdgeRow from '../components/CausalEdgeRow.vue'
import InquiryChain from '../components/InquiryChain.vue'
import LevelBadge from '../components/LevelBadge.vue'
import VerdictBadge from '../components/VerdictBadge.vue'
import AsyncSection from '../components/ui/AsyncSection.vue'
import AssistantPanel from '../components/AssistantPanel.vue'
import Callout from '../components/ui/Callout.vue'
import DataTable from '../components/ui/DataTable.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import EvidenceCite from '../components/EvidenceCite.vue'
import TechDetails from '../components/TechDetails.vue'

const route = useRoute()
const event = ref(null)
const error = ref('')
const advice = ref(null)

async function load(id) {
  event.value = null
  error.value = ''
  advice.value = null
  try {
    event.value = await api.event(id)
  } catch (exception) {
    error.value = exception.message
    return
  }
  // The recommendation endpoint answers for every open alert of a company at
  // once, so it is a second request rather than part of the event payload. A
  // failure here must not take the page down: the advice is an addition to ⑦,
  // not its subject.
  if (!event.value.alerts?.length) return
  try {
    // 只要这个事件自己那几条预警的建议。此前这里把该公司全部未处置预警（182 条）取回来
    // 再在前端筛出一条，服务端为此逐条重算并逐条 commit——这一页因此要 1.24 秒。
    const body = await api.notifyDispositions({
      company: event.value.company_id,
      alert_id: event.value.alerts.map((alert) => alert.alert_id),
    })
    const byAlert = Object.fromEntries(body.items.map((row) => [row.alert_id, row]))
    advice.value = event.value.alerts.map((alert) => byAlert[alert.alert_id]).find(Boolean) || null
  } catch {
    advice.value = null
  }
}

watch(() => route.params.id, load, { immediate: true })

const percent = (value) => `${(value * 100).toFixed(0)}%`

const ELEMENT_TYPE_LABELS = {
  subject: '事件主体', time: '时间', amount: '金额', ratio: '比例',
  person: '人物', organization: '机构', product: '产品',
}

const ELEMENT_COLUMNS = [
  { key: 'element_type', title: '类型', slot: 'type', nowrap: true, width: '92px' },
  { key: 'raw_text', title: '原文' },
  { key: 'normalized_value', title: '标准化', slot: 'normalized' },
  { key: 'unit', title: '单位', slot: 'unit', nowrap: true, width: '72px' },
  { key: 'basis', title: '依据', slot: 'basis', nowrap: true, width: '96px' },
]

const normalizedCount = computed(() =>
  (event.value?.elements || []).filter((element) => element.status === 'normalized').length)

/** 这一段回答的是页面此前答不出的问题：这件事之前发生了什么、之后引出了什么。
 *
 *  接口只发五类方向性关系；被判为「仅背景相关」的候选是这一层的拒绝票，留在
 *  库里当度量，不画到页面上——把「两件事只是共现」渲染成一条线，正是这一层
 *  要修的毛病。
 *
 *  一个事件可以有三十多条边（实测某条药明公告 31 条后果），全列出来会把这一块
 *  撑成整页。折叠沿用传播图的做法：先给最近的几条，其余收进一句「另有 N 条」，
 *  收起而不是丢掉。 */
const CAUSAL_VISIBLE = 6
const causalColumns = computed(() => {
  const edges = event.value?.causal_edges || []
  return [
    { side: 'cause', title: '前因', empty: '没有指向该事件的因果边。' },
    { side: 'effect', title: '后果', empty: '尚未有从该事件引出的因果边。' },
  ].map((column) => {
    const own = edges.filter((edge) => edge.side === column.side)
    return { ...column, edges: own, shown: own.slice(0, CAUSAL_VISIBLE),
             rest: own.slice(CAUSAL_VISIBLE) }
  })
})

/** 市场同期表现，作为影响方向的外部对照。
 *
 *  三条口径写在这里，因为它们决定了这个数字能说什么：取的是**超额**收益而非
 *  原始收益（跌 2% 而大盘跌 3%，相对是涨的）；锚在**公开日**而非事件发生日
 *  （市场是知道之后才反应的）；小于一个标准差的移动记为「未见明显反应」，
 *  不折算成「中性」——那是两种不同的断言。 */
const BENCHMARK_LABELS = { sh000300: '沪深300', hkHSI: '恒生指数' }
const signed = (value) => `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
const reaction = computed(() => {
  const row = event.value?.market_reaction
  if (!row || row.car_1 === null || row.car_1 === undefined) return null
  const tone = row.significant ? (row.car_1 > 0 ? 'up' : 'down') : 'flat'
  return {
    tone,
    text: row.significant ? `超额 ${signed(row.car_1)}` : `未见明显反应（${signed(row.car_1)}）`,
    car: signed(row.car_1),
    sigma: `σ=${(row.sigma * 100).toFixed(2)}%`,
    symbol: row.symbol,
    benchmarkLabel: BENCHMARK_LABELS[row.benchmark_symbol] || row.benchmark_symbol,
  }
})

const partKeys = CREDIBILITY_PARTS.map((part) => part.key)

/** weight × value per part — the contributions, which is what sums to the score. */
const contributions = computed(() => {
  const basis = event.value?.alerts?.[0]?.credibility_basis
  if (!basis) return []
  return CREDIBILITY_PARTS.map((part) => ({
    key: part.key,
    label: part.label,
    weight: part.weight,
    value: basis[part.key] ?? 0,
    count: (basis[part.key] ?? 0) * part.weight,
  }))
})
</script>

<template>
  <AsyncSection
    :loading="!event && !error" :error="error" :empty="false"
    empty-text="未找到该事件，链接可能来自其他批次的采集。"
  >
    <PageHeader
      :title="event.summary"
      note="该事件的七项结构化输出，缺失项均予以标明。"
      :crumbs="[{ label: '事件库', to: '/events' },
                { label: companyLabel(event.company_id), to: `/events?company=${event.company_id}` }]"
    >
      <template #actions>
        <!-- 这条事件是从哪篇稿子来的。接口一直返回 canonical_url，而这一页——
             整个平台回答「可核验」的核心页——此前一次都没用过它。 -->
        <a v-if="event.canonical_url" :href="event.canonical_url"
           target="_blank" rel="noopener">看原报道 →</a>
        <RouterLink :to="`/report/events/${event.event_id}`">导出报告 →</RouterLink>
        <a href="#assistant">问研判助手 ↓</a>
      </template>
    </PageHeader>

    <!-- 命题七项输出，按顺序摆在一页上：摘要、主体、影响对象、可信度、证据、
         时间线入口、预警理由。缺哪一项就明说缺，不用别的数字顶替。 -->
    <div class="grid">
      <!-- 案例区：这一页在讲的那条记录本身。②–⑦ 是系统对它的判断与依据。 -->
      <section class="card subject">
        <h2>① 摘要</h2>
        <p class="body">{{ event.summary }}</p>
        <dl class="detail-grid">
          <dt>事件类型</dt>
          <!-- 这三行是答辩核心页的第一屏，此前全是裸标签词：不给理由、不给命中的原文、
               不给置信度、也不说是规则判的还是模型判的——而同一页往下的可信度有四项贡献条、
               证据有页码与原文、因果边有一句理由和两端引证。依据默认收起，见 BasisDetails。 -->
          <dd>
            <BasisDetails :value="event.event_types.map(eventTypeLabel).join('、')"
                          :basis="event.basis?.event_type" :label-source="event.label_source" />
          </dd>
          <dt>专题</dt>
          <dd>
            <span v-for="code in event.issue_codes" :key="code" class="tag">{{ issueLabel(code) }}</span>
            <span v-if="!event.issue_codes.length">—</span>
          </dd>
          <dt>生产方式</dt>
          <dd>
            <!-- 给一家公司的稿件贴上「疑似宣发」而不说为什么，是最容易被点名的一处；
                 理由早就在 production_mode_labels 里，只差这一个 JOIN。 -->
            <BasisDetails :value="PRODUCTION_LABELS[event.production_mode] || event.production_mode"
                          :basis="event.basis?.production_mode" :label-source="event.label_source" />
          </dd>
        </dl>
      </section>

      <section class="card">
        <h2>② 事件主体　③ 影响对象</h2>
        <dl class="detail-grid">
          <dt>主体</dt>
          <dd>{{ event.event_subjects.join('、') || '—' }}</dd>
          <dt>影响对象</dt>
          <dd>{{ event.affected_objects.map(impactObjectLabel).join('、') || '—' }}</dd>
          <dt>影响方向</dt>
          <dd>
            <BasisDetails :value="impactText(event.impact_direction, event.impact_magnitude)"
                          :basis="event.basis?.event_impact" :label-source="event.label_source" />
            <!-- 影响方向此前没有任何外部对照物：模型说正面就是正面。
                 这里给出同期相对基准指数的超额收益，作为可证伪的旁证。 -->
            <span v-if="reaction" class="reaction" :class="reaction.tone">
              <!-- 基准与窗口是这个数的单位，不是对它的解释：不说明对标什么，
                   「超额 +4.12%」本身没有意义。 -->
              市场同期 {{ reaction.text }}（对{{ reaction.benchmarkLabel }}，1 日）
            </span>
          </dd>
          <dt>语言倾向</dt>
          <dd>
            <BasisDetails :value="sentimentLabel(event.article_sentiment)"
                          :basis="event.basis?.article_sentiment" :label-source="event.label_source" />
          </dd>
          <dt v-if="event.amounts.length">金额</dt>
          <dd v-if="event.amounts.length">{{ event.amounts.join('、') }}</dd>
          <dt v-if="event.organizations.length">相关机构</dt>
          <dd v-if="event.organizations.length">{{ event.organizations.join('、') }}</dd>
        </dl>

        <p v-if="!reaction && (event.event_time || event.published_at)"
           class="muted small reaction-note">无市场对照：该事件不在已采集的行情区间内。</p>

        <!-- 标准化那一半：上面的列表只有值，这里是每个值归一到了什么、
             用什么单位、依据是什么、以及是否归一成功。 -->
        <details v-if="event.elements?.length" class="elements">
          <summary>
            要素抽取与标准化（{{ event.elements.length }} 项，已归一 {{ normalizedCount }} 项）
          </summary>
          <DataTable
            :columns="ELEMENT_COLUMNS" :rows="event.elements" :row-key="(row, index) => index"
          >
            <template #type="{ row }">
              {{ ELEMENT_TYPE_LABELS[row.element_type] || row.element_type }}
            </template>
            <template #normalized="{ row }">
              <span v-if="row.status === 'normalized'" class="mono">{{ row.normalized_value }}</span>
              <span v-else class="muted">未解析——保留原文</span>
            </template>
            <template #unit="{ row }"><span class="mono">{{ row.unit || '—' }}</span></template>
            <template #basis="{ row }"><span class="muted">{{ BASIS_LABELS[row.basis] || row.basis || '—' }}</span></template>
          </DataTable>
        </details>
      </section>

      <section class="card">
        <h2>④ 可信度</h2>
        <template v-if="event.alerts.length">
          <div class="score-line">
            {{ CREDIBILITY_LABELS[event.alerts[0].credibility_label] || event.alerts[0].credibility_label }}
            <b class="mono">{{ event.alerts[0].credibility.toFixed(2) }}</b>
          </div>
          <EChart
            :builder="(tokens) => partBar(tokens, { parts: contributions, stableKeys: partKeys })"
            :label="`可信度 ${event.alerts[0].credibility.toFixed(2)} 的四项构成，按贡献值堆叠`"
            :height="88"
            :table-columns="['构成', '权重', '取值', '贡献']"
            :table-rows="contributions.map((part) => [
              part.label, percent(part.weight), part.value.toFixed(2), part.count.toFixed(3),
            ])"
          />
        </template>
        <!-- Without an alert there is no credibility score; the model's own
             extraction confidence is a different quantity and is labelled so. -->
        <template v-else>
          <!-- 前一句解释的是「这个字段为什么是空的」——空态，留在外面；抽取置信度
               是模型自报的数，连同那句限定语一起收进折叠。 -->
          <p class="muted small">该事件未触发预警，因此没有窗口级可信度评分。</p>
          <TechDetails :items="[
            { label: '系统抽取置信度', value: event.extraction_confidence?.toFixed?.(2) ?? '—' },
          ]">
            <!-- 这句话被 event-detail.test.js 的「不能被改掉的两句话」逐字钉着，
                 措辞不动，只是换了位置：原生 details 的折叠态文本仍在 DOM 里。 -->
            系统自评，非人工核验结果。
          </TechDetails>
        </template>
      </section>

    </div>

    <section class="card block">
      <h2>⑤ 证据：原子主张与裁决</h2>
      <!-- 外链是「去发布方那里核对」，不是「平台存的就是这一版」。两者的区别在
           文件被替换或下架时才显出来，而那时读者正拿着一个对不上的页面。 -->
      <TechDetails v-if="event.claims.length" hint="链接说明">
        文件名链接指向发布方当前版本（巨潮资讯、交易所或公司官网），并定位至引用页。
        平台引证的是采集时的版本，正文与 SHA-256 均保存在证据层；如文件被替换或下架，
        链接可能失效或指向新版本，但上文引用内容不受影响。两者不一致时，以「展开本页原文」
        为准。
      </TechDetails>
      <div v-if="!event.claims.length" class="muted small">
        该事件没有可定位的原子主张，已排除在核验成功率分母之外。
      </div>
      <!-- 模型提过、但在原文里定位不到的那几句。此前只记「丢了 N 条」不记内容，
           于是分不清是模型在转述（该丢）还是定位器太严（是缺陷）——把原话摆出来，
           这个判断几秒钟就能下。 -->
      <details v-if="event.dropped_claims?.length" class="dropped">
        <summary>
          抽取结果中未能在原文定位的 {{ event.dropped_claims.length }} 句（未纳入证据）
        </summary>
        <ul>
          <li v-for="(quote, index) in event.dropped_claims" :key="index" class="raw">{{ quote }}</li>
        </ul>
      </details>
      <div v-for="claim in event.claims" :key="claim.claim_id" class="claim">
        <VerdictBadge :verdict="claim.verdict" :subtype="claim.partial_subtype" />
        <div class="claim-body">
          <div>
            {{ claim.text }}
            <!-- 主张性质此前存在库里、发在 API 里，却从没到过页面；而它恰恰决定读者
                 怎么取信这一句。悬停给判据，不占常驻正文。 -->
            <span v-if="claim.claim_type" class="tag rule claim-type"
                  :title="CLAIM_TYPE_HINTS[claim.claim_type] || ''">
              {{ claimTypeLabel(claim.claim_type) }}
            </span>
          </div>
          <EvidenceCite
            v-if="claim.evidence_page"
            :title="claim.evidence_title" :url="claim.evidence_url"
            :page="claim.evidence_page" :section="claim.evidence_section"
            :note="METHOD_LABELS[claim.verdict_method] || claim.verdict_method"
            :text="claim.evidence_text" />
          <div v-if="claim.verdict_reason" class="muted small reason">{{ claim.verdict_reason }}</div>
          <InquiryChain v-if="claim.inquiry" :inquiry="claim.inquiry" compact />
        </div>
      </div>
    </section>

    <section class="card block">
      <h2>⑥ 时间线与阶段风险</h2>
      <template v-if="event.timeline">
        <div class="score-line">
          <LevelBadge :level="event.timeline.adjusted_risk_level"
                      :score="event.timeline.adjusted_risk_score" />
        </div>
        <p class="formula mono">
          {{ event.timeline.base_risk_score.toFixed(1) }} × {{ event.timeline.phase_coefficient }}
          （{{ event.timeline.phase_label }}）= {{ event.timeline.adjusted_risk_score.toFixed(1) }}
        </p>
        <dl class="detail-grid">
          <dt>时间口径</dt>
          <dd>{{ formatDate(event.timeline.timeline_at) }} · {{ basisLabel(event.timeline.time_basis) }}
            <template v-if="event.target_time"> · {{ targetText(event.target_time) }}</template></dd>
          <dt>新增事实</dt>
          <dd>{{ event.timeline.new_fact_count }} / {{ event.timeline.claim_count }} 条主张</dd>
        </dl>
        <RouterLink class="small" to="/timeline">在时间线中查看 →</RouterLink>
      </template>
      <p v-else class="muted small">该事件没有时间线记录。</p>
    </section>

    <section class="card block">
      <h2>⑦ 预警理由与处置建议</h2>
      <!-- 风险分的七项加减此前一项都没存过库、一处都没展示过：读者看到的是「高 85」，
           而旁边的可信度从写下第一天起就摊开它的四项加权贡献。这里同样默认收起。 -->
      <details v-if="event.risk_breakdown?.length" class="risk-basis">
        <summary class="small">
          风险分 <b class="mono">{{ event.risk_score?.toFixed?.(0) }}</b>
          <span class="muted">的七项加减</span>
        </summary>
        <RiskBreakdown :parts="event.risk_breakdown" :score="event.risk_score" />
        <p class="muted small">
          分档阈值 75 / 45 及七项权重尚未经标注数据标定；此处仅说明该分数的计算方式。
        </p>
      </details>
      <div v-if="!event.alerts.length" class="muted small">该事件未触发任何预警规则。</div>
      <Callout v-else-if="advice" tone="info" :title="`建议：${advice.recommended_label}`">
        {{ advice.why }}
        <div class="basis">
          <span v-for="item in advice.basis" :key="item.code" class="basis-item">
            <span class="muted small">{{ item.label }}</span>
            <span class="small">{{ item.value }}</span>
          </span>
        </div>
      </Callout>
      <div v-for="alert in event.alerts" :key="alert.alert_id" class="alert-row">
        <div class="alert-head">
          <RouterLink :to="`/alerts?issue=${alert.issue_code || ''}`">{{ alert.headline }}</RouterLink>
          <LevelBadge :level="alert.risk_level" :score="alert.adjusted_risk_score" />
        </div>
        <div>
          <span v-for="reason in alert.reasons" :key="reason.code" class="tag rule">{{ reason.label }}</span>
        </div>
        <div class="muted small">
          窗口 {{ formatDate(alert.window_start) }}—{{ formatDate(alert.window_end) }} ·
          合并 {{ alert.event_count }} 篇报道
        </div>
      </div>
    </section>

    <section v-if="event.causal_edges?.length" class="card block">
      <h2>前因与后果</h2>
      <div class="grid">
        <div v-for="column in causalColumns" :key="column.side">
          <h3 class="sub">{{ column.title }} · {{ column.edges.length }}</h3>
          <p v-if="!column.edges.length" class="muted small">{{ column.empty }}</p>
          <CausalEdgeRow v-for="edge in column.shown" :key="edge.edge_id" :edge="edge" />
          <details v-if="column.rest.length">
            <summary class="small">另有 {{ column.rest.length }} 条未展开</summary>
            <CausalEdgeRow v-for="edge in column.rest" :key="edge.edge_id" :edge="edge"
                           :quotes="false" />
          </details>
        </div>
      </div>
    </section>

    <section v-if="event.propagation.length || event.duplicate_cluster" class="card block">
      <h2>传播链路与同源版本</h2>
      <div v-for="chain in event.propagation" :key="chain.component_id" class="muted small">
        <RouterLink :to="`/propagation?version=${event.version_id || ''}`">
          该报道所在链路共 {{ chain.member_count }} 个版本、{{ chain.source_count }} 个来源
        </RouterLink>
        （{{ formatDate(chain.first_published_at) }} → {{ formatDate(chain.last_published_at) }}）
      </div>
      <!-- 同源簇此前在这一页完全不存在：读者看得到「传播链路」，看不到「这篇稿子
           还有几个近重复版本」，也走不到那份成员名单。 -->
      <div v-if="event.duplicate_cluster" class="muted small">
        <RouterLink :to="`/duplicates?cluster=${event.duplicate_cluster.cluster_id}`">
          该报道有 {{ event.duplicate_cluster.member_count }} 个同源版本、
          来自 {{ event.duplicate_cluster.distinct_sources }} 家发布方 →
        </RouterLink>
        <template v-if="event.duplicate_cluster.distinct_sources === 1">
          单一来源——同一家反复发布，不构成多源印证。
        </template>
      </div>
    </section>

    <!-- 研判助手在七项输出之后：编号是与命题输出要求的契约，加小节可以、插队不行。
         页头有一个「问研判助手 ↓」跳过来。 -->
    <AssistantPanel scope="event" :object-id="event.event_id" />
  </AsyncSection>
</template>

<style scoped>
.sub { font-size: var(--fs-note); font-weight: 600; margin: 0 0 6px; color: var(--text-muted); }
summary { cursor: pointer; color: var(--text-muted); }
.basis { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-top: 8px; }
.basis-item { display: flex; gap: 6px; align-items: baseline; }
h2 { font-size: var(--fs-aux); font-weight: 600; color: var(--text-muted); text-transform: uppercase;
  letter-spacing: 0.04em; margin: 0 0 10px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); gap: 12px; }
.block { margin-top: 12px; }
.body { font-size: var(--fs-body); margin: 0 0 12px; line-height: 1.75; }
.score-line { font-size: var(--fs-body); margin-bottom: 8px; }
.formula { font-size: var(--fs-body); margin: 0 0 10px; }
/* 不着色。第一版把跌染成 `--status-good`（绿），理由是 A 股红涨绿跌——但那
   是把一个**保留状态色**挪去表示涨跌，正是本项目自己禁止的事（状态色只归
   good/warning/serious/critical，不得改作他用）；何况同一个绿在中西读者眼里
   一个是「跌」一个是「好」。方向由符号和「超额 / 未见明显反应」这几个字承担，
   本来就不需要颜色。未见明显反应时再淡一档，这是唯一的视觉区分。 */
.reaction { margin-left: 8px; font-size: var(--fs-note); padding: 1px 6px; border-radius: 4px;
  border: 1px solid var(--gridline); color: var(--text-secondary); white-space: nowrap; }
.reaction.flat { color: var(--text-muted); }
/* 窄屏下这枚标签比值列还宽，不折行就把整页撑出横向滚动。 */
@media (max-width: 760px) { .reaction { white-space: normal; margin-left: 0; } }
.reaction-note { margin: 10px 0 0; line-height: 1.7; }
.dropped { margin-top: 10px; font-size: var(--fs-aux); }
.dropped summary { cursor: pointer; color: var(--text-muted); }
.dropped ul { list-style: none; margin: 8px 0 0; padding: 0; }
.dropped li { padding: 5px 0 5px 10px; border-left: 2px solid var(--gridline);
  margin-bottom: 4px; color: var(--text-secondary); line-height: 1.65; }
.claim { display: flex; gap: 10px; padding: 9px 0; border-top: 1px solid var(--gridline); font-size: var(--fs-body); }
.claim:first-of-type { border-top: none; }
.claim-body { min-width: 0; }
.reason { margin-top: 5px; }
.claim-type { margin-left: 6px; }
.risk-basis { margin-bottom: 10px; }
.risk-basis > summary { cursor: pointer; }
.alert-row { padding: 9px 0; border-top: 1px solid var(--gridline); }
.alert-row:first-of-type { border-top: none; }
.alert-head { display: flex; justify-content: space-between; gap: 14px; align-items: baseline; margin-bottom: 6px; }
.tag.rule { color: var(--status-serious); border-color: var(--status-serious); }
.elements { margin-top: 12px; }
.elements summary { cursor: pointer; font-size: var(--fs-aux); color: var(--text-secondary); }
.elements summary:hover { color: var(--text-primary); }
.elements table { margin-top: 8px; font-size: var(--fs-aux); }
</style>
