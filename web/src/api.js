import { reactive } from 'vue'

import { markSignedOut } from './session.js'

const BASE = '/api'

const STATUS_TEXT = {
  400: '请求有误，请检查后重试', 401: '未登录或会话已过期', 403: '当前账号没有此操作的权限',
  404: '要找的内容不存在或已被移除', 409: '内容已被他人修改，请刷新后重试',
  413: '提交的内容过大', 422: '提交的内容不完整或格式不对', 429: '操作过于频繁，请稍后再试',
}
const HAS_CHINESE = /[\u4e00-\u9fff]/

/** 读者看得懂的一句话，外加一个可以报给运维的编号（I-06）。
 *  服务端写的中文原样给出；英文、结构化的校验错误、浏览器自己的「Failed to fetch」
 *  一律换成通用说法——它们说的是实现，不是读者能做什么。 */
export function friendlyError(status, payload, requestId) {
  const detail = payload?.detail
  const text = typeof detail === 'string' && HAS_CHINESE.test(detail)
    ? detail : (STATUS_TEXT[status] || '服务暂时不可用，请稍后重试')
  const reference = requestId || payload?.request_id
  const error = new Error(reference ? `${text}（编号 ${reference}）` : text)
  error.status = status
  return error
}

async function call(url, init) {
  try {
    return await fetch(url, init)
  } catch {
    throw new Error('网络连接异常，请检查网络后重试')
  }
}

async function readJson(response) {
  try {
    return await response.json()
  } catch {
    throw friendlyError(response.status, null, response.headers.get('X-Request-ID'))
  }
}

async function request(path, params = {}) {
  const query = new URLSearchParams()
  const put = (key, value) => {
    if (value !== null && value !== undefined && value !== '') query.append(key, value)
  }
  for (const [key, value] of Object.entries(params)) {
    // 数组按「同名参数重复出现」序列化，而不是 join 成一个字符串——服务端按重复参数解析，
    // 这样一个页面要 N 条记录时仍然只发一个请求。
    if (Array.isArray(value)) value.forEach((item) => put(key, item))
    else put(key, value)
  }
  const suffix = query.toString() ? `?${query}` : ''
  const response = await call(`${BASE}${path}${suffix}`)
  if (!response.ok) {
    // An expired session must present itself as one; without this the pages
    // would fill with "请求失败（401）" and hide the actual remedy.
    if (response.status === 401) markSignedOut()
    const detail = await response.json().catch(() => ({}))
    throw friendlyError(response.status, detail, response.headers.get('X-Request-ID'))
  }
  return readJson(response)
}

/** POST lives in its own helper rather than being an option on every read, so
 *  that the handful of things the client may write to stay visible as a list:
 *  human review, push subscriptions and their dispatch, alert dispositions, and
 *  issue reassignment.  (The assistant also POSTs, but only to ask: it writes
 *  one audit row and no business data.) Everything else is a read, and the server opens the
 *  database read-only to keep it that way. */
async function send(path, body) {
  const response = await call(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401) markSignedOut()
    throw friendlyError(response.status, payload, response.headers.get('X-Request-ID'))
  }
  return payload
}

/** Server-Sent Events over a POST: ``data: {...}`` blocks, one per finished step, the
 *  last one carrying the result.  EventSource cannot POST, so the body is read by hand. */
async function stream(path, body, onStep) {
  const response = await call(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body ?? {}),
  })
  if (!response.ok) {
    if (response.status === 401) markSignedOut()
    const payload = await response.json().catch(() => ({}))
    throw friendlyError(response.status, payload, response.headers.get('X-Request-ID'))
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result = null
  const take = (block) => {
    const line = block.split('\n').find((part) => part.startsWith('data: '))
    if (!line) return
    const event = JSON.parse(line.slice(6))
    if (event.type === 'step') onStep?.(event.step)
    else if (event.type === 'result') result = event.result
  }
  for (;;) {
    const { done, value } = await reader.read()
    if (value) buffer += decoder.decode(value, { stream: !done })
    let cut
    while ((cut = buffer.indexOf('\n\n')) !== -1) {
      take(buffer.slice(0, cut))
      buffer = buffer.slice(cut + 2)
    }
    if (done) break
  }
  if (buffer.trim()) take(buffer)
  if (!result) throw new Error('回答中断了，请稍后重试')
  return result
}

export const api = {
  health: () => request('/health'),
  systemStatus: () => request('/system/status'),
  company: (id) => request(`/companies/${encodeURIComponent(id)}`),
  vocabularies: () => request('/vocabularies'),
  companies: () => request('/companies'),
  financials: () => request('/financials'),
  overview: () => request('/overview'),
  guide: () => request('/guide'),
  search: (params) => request('/search', params),
  users: () => request('/users'),
  userCreate: (body) => send('/users', body),
  userRole: (username, role) => send(`/users/${username}/role`, { role }),
  userResetPassword: (username, password) => send(`/users/${username}/password`, { password }),
  userActive: (username, active) => send(`/users/${username}/active`, { active }),
  changeOwnPassword: (current, password) => send('/users/me/password', { current, password }),
  overviewPipeline: () => request('/overview/pipeline'),
  overviewTrend: (params) => request('/overview/trend', params),
  events: (params) => request('/events', params),
  event: (id) => request(`/events/${id}`),
  claims: (params) => request('/claims', params),
  claim: (id) => request(`/claims/${id}`),
  claimStats: (params) => request('/claims/stats', params),
  diffs: (params) => request('/diffs', params),
  diffFacets: (params) => request('/diffs/facets', params),
  issues: (params) => request('/issues', params),
  issue: (id) => request(`/issues/${id}`),
  issueAdjustments: (params) => request('/issues/adjustments/log', params),
  issueReassign: (body) => send('/issues/reassign', body),
  issueUndoAdjustment: (id) => send(`/issues/adjustments/${id}/undo`),
  alerts: (params) => request('/alerts', params),
  alert: (id) => request(`/alerts/${id}`),
  risks: (params) => request('/risks', params),
  riskSummary: (params) => request('/risks/summary', params),
  riskInvestigations: (params) => request('/risks/investigations', params),
  risk: (id) => request(`/risks/${id}`),
  // 上报对象：有复核权的账号，只有名字与角色。分析师可读，账号列表本身仍只对管理员开放。
  reviewers: () => request('/users/reviewers'),
  tasks: () => request('/risks/tasks'),
  auditDispositions: (params) => request('/audit/dispositions', params),
  alertFacets: (params) => request('/alerts/facets', params),
  timeline: (params) => request('/timeline', params),
  phases: (params) => request('/timeline/phases', params),
  rounds: () => request('/timeline/rounds'),
  components: (params) => request('/propagation', params),
  component: (id) => request(`/propagation/${id}`),
  propagationStats: (params) => request('/propagation/stats', params),
  evidencePage: (versionId, page) => request(`/evidence/${versionId}/page/${page}`),
  auditRuns: (params) => request('/audit/runs', params),
  auditActions: (params) => request('/audit/actions', params),
  auditDecisions: (params) => request('/audit/decisions', params),
  auditEvents: (params) => request('/audit/events', params),
  notifications: (params) => request('/notifications', params),
  markNotificationsRead: (ids) => send('/notifications/read', ids ? { notification_ids: ids } : {}),
  // 研判助手：POST 只是因为问题可能很长，不该进地址栏；它不写任何业务数据。
  askAssistant: (scope, id, question) => send('/assistant/ask', { scope, id, question }),
  // 同一件事的流式版：每做完一步回调 onStep，返回最终结果。
  askAssistantStream: (scope, id, question, onStep) => stream('/assistant/stream', { scope, id, question }, onStep),
  benchmarkSummary: () => request('/benchmark/summary'),
  benchmarkRuns: (params) => request('/benchmark/runs', params),
  benchmarkRun: (id) => request(`/benchmark/runs/${id}`),
  benchmarkDataset: () => request('/benchmark/dataset'),
  sentimentLabels: (params) => request('/profiling/sentiment', params),
  sentimentStats: (params) => request('/profiling/sentiment/stats', params),
  productionLabels: (params) => request('/profiling/production', params),
  productionStats: (params) => request('/profiling/production/stats', params),
  ingestionFunnel: (params) => request('/ingestion/funnel', params),
  ingestionLayers: (params) => request('/ingestion/layers', params),
  ingestionSources: (params) => request('/ingestion/sources', params),
  ingestionDocuments: (params) => request('/ingestion/documents', params),
  elements: (params) => request('/elements', params),
  elementStats: (params) => request('/elements/stats', params),
  duplicates: (params) => request('/duplicates', params),
  duplicateStats: (params) => request('/duplicates/stats', params),
  duplicate: (id) => request(`/duplicates/${id}`),
  notifySubscriptions: () => request('/notify/subscriptions'),
  notifyCreate: (body) => send('/notify/subscriptions', body),
  notifyToggle: (id, body) => send(`/notify/subscriptions/${id}/toggle`, body),
  // These two take query parameters rather than a body, and they are the only
  // POSTs in the app that do; building the string here keeps that detail out of
  // the pages.
  notifyEvaluate: ({ company } = {}) =>
    send(`/notify/evaluate${company ? `?company=${encodeURIComponent(company)}` : ''}`),
  notifyFlush: ({ limit = 50 } = {}) => send(`/notify/flush?limit=${limit}`),
  notifyOutbox: (params) => request('/notify/outbox', params),
  notifyDispositions: (params) => request('/notify/dispositions', params),
  notifyDecide: (alertId, body) => send(`/notify/dispositions/${alertId}`, body),
  notifyDispositionLog: (params) => request('/notify/dispositions/log', params),
  reviewTasks: (params) => request('/review/tasks', params),
  reviewMetrics: () => request('/review/metrics'),
  reviewSeed: (body) => send('/review/seed', body),
  reviewJudge: (taskId, body) => send(`/review/tasks/${taskId}/judge`, body),
  reviewApprove: (taskId) => send(`/review/tasks/${taskId}/approve`),
  reviewReject: (taskId, body) => send(`/review/tasks/${taskId}/reject`, body),
  reviewReviewers: () => request('/review/reviewers'),
  reviewAssign: (body) => send('/review/assign', body),
}

export const JUDGEMENT_LABELS = { correct: '判定正确', wrong: '判定错误', unsure: '无法判断' }
export const TASK_STATUS_LABELS = {
  open: '待复核', judged: '已判定', approved: '已通过', rejected: '已退回',
}
export const TASK_KIND_LABELS = { verdict: '主张裁决', event: '事件抽取', diff: '披露差异' }

/** 公司名只从配置来，经公开的 `/api/health` 下发；这里不写任何一家公司。
 *
 *  此前这里是一张写死的两行表，后端 Word 报告里还有另一张、登录页封面上又写了一遍——
 *  加第三家公司，全站就会把它显示成内部标识，登录页永远只说两家。现在是一个响应式对象：
 *  App 启动时（登录页上也是）填一次，所有 `companyLabel()` 的调用处随之更新，签名不变。
 *  `tests/test_migration` 扫描前端与接口代码，不许公司标识或显示名再写回来。 */
export const COMPANY_LABELS = reactive({})
export const companyLabel = (id) => COMPANY_LABELS[id] || id || '—'

export function setCompanyLabels(companies = []) {
  for (const company of companies) COMPANY_LABELS[company.company_id] = company.name
}

let labelsLoading = null
/** 只取一次；失败了下次再试，而不是把一次网络抖动记成「没有公司」。 */
export function loadCompanyLabels() {
  labelsLoading ||= request('/health')
    .then((body) => { setCompanyLabels(body.companies); return COMPANY_LABELS })
    .catch(() => { labelsLoading = null; return COMPANY_LABELS })
  return labelsLoading
}

/** Verdict presentation. Each carries an icon and a label so the colour is
 *  never the only thing distinguishing one state from another. */
export const VERDICT_META = {
  SUPPORTED: { label: '支持', icon: '✓', color: 'var(--status-good)' },
  PARTIAL: { label: '部分支持', icon: '◐', color: 'var(--status-warning)' },
  CONFLICT: { label: '冲突', icon: '✕', color: 'var(--status-critical)' },
  UNVERIFIABLE: { label: '无法核验', icon: '○', color: 'var(--status-neutral)' },
}

/** Mirrors api/review_db.py CORRECT_LABELS: what a reviewer can say the answer
 *  should have been.  Without it a "wrong" gives an accuracy figure and nothing
 *  else — no confusion matrix, and therefore no macro-F1. */
export const CORRECT_LABELS = {
  verdict: ['SUPPORTED', 'PARTIAL', 'CONFLICT', 'UNVERIFIABLE'],
  event: ['low', 'medium', 'high'],
  diff: ['numeric', 'modified', 'added', 'removed', 'not_a_change'],
}

/** 议题状态。IssueDetailView 与报告页各写过一份，报告页那份是裸枚举。 */
export const ISSUE_STATUS_LABELS = {
  emerging: '新出现', escalating: '升温', active: '活跃', cooling: '降温', dormant: '沉寂',
}

/** 要素归一的依据强度：原文明写的时间，和按发布时间推算的时间，不是同一强度。
 *  主体的四条依据同理——照抄别名、认出自己的 id、顺着「本公司」找回文档主体、
 *  查机构名册，可信程度依次递减，页面上要看得出用的是哪一条。 */
export const BASIS_LABELS = {
  explicit: '原文明写',
  relative_to_publication: '按发布时间推算',
  alias: '别名表命中',
  company_id_literal: '主体标识原样',
  document_company: '按文档主体还原',
  organization_registry: '机构名册命中',
  organization_branch: '机构分支归并',
  未记录: '未记录依据',
}

/** 归一值所在的命名空间或计量单位。货币代码本就该原样示人，映射只覆盖那些
 *  不该出现在页面上的内部标识——`company_id` 与 `org_id` 是两套主键，不是一套。 */
export const UNIT_LABELS = {
  company_id: '受监控公司',
  org_id: '机构',
  name: '姓名',
  percent: '百分比',
  percentage_point: '百分点',
}

/** 同源聚类的方式。目前只有一种，但页面上不该印 `near_duplicate`。 */
export const CLUSTER_KIND_LABELS = { near_duplicate: '近重复' }

/** 风险等级的唯一一套名字。见 LEVEL_META——那份表只多给颜色，标签取自同一组词。 */
export const RISK_LEVEL_LABELS = {
  low: '一般关注', medium: '中风险', high: '高风险',
}

/** Delivery state. Colour is paired with a glyph and a label everywhere it is
 *  shown, as the verdict and level badges already are. */
/** 处置状态：待研判 → 核查中 → 待上级复核 → 已归档，旁路「持续跟踪」，复核可退回「退回补证」。
 *
 *  与 `api/dispositions.py` 的 STATUSES 是同一组码，标签也是同一组词——这里写的
 *  是图标与颜色，服务端写的是流程，两边不重复定义同一个量的含义。
 *
 *  归档另需核查负责人角色，服务端强制；界面上把它标出来而不是藏起来——
 *  一个看不见的能力和一个不存在的能力，对使用者是同一件事。
 *
 *  合法去向由服务端随每次处置返回（`transitions`），界面不自己再实现一遍流程：
 *  两份实现迟早会各说各话。 */
export const DISPOSITION_STATUS_META = {
  pending: { label: '待研判', icon: '○', color: 'var(--status-warning)' },
  investigating: { label: '核查中', icon: '◐', color: 'var(--series-1)' },
  reporting: { label: '待上级复核', icon: '▲', color: 'var(--status-serious)' },
  returned: { label: '退回补证', icon: '↩', color: 'var(--status-critical)' },
  tracking: { label: '持续跟踪', icon: '◇', color: 'var(--status-neutral)' },
  archived: { label: '已归档', icon: '⊟', color: 'var(--text-muted)', requires: 'reviewer' },
}

export const DELIVERY_STATUS_META = {
  queued: { label: '待发送', icon: '○', color: 'var(--status-neutral)' },
  dry_run: { label: '演示模式', icon: '◐', color: 'var(--series-1)' },
  sent: { label: '已送达', icon: '✓', color: 'var(--status-good)' },
  failed: { label: '发送失败', icon: '✕', color: 'var(--status-critical)' },
  suppressed: { label: '已抑制', icon: '—', color: 'var(--status-warning)' },
}
/** Why a delivery was queued — the M6-10 half of the feature. */
export const DELIVERY_REASON_LABELS = {
  new: '首次触发', escalated: '风险上调', downgraded: '风险下调',
  withdrawn: '预警撤销', updated: '窗口内新增事件',
}
/** Why one was not sent. Shown in full: what did not go out matters as much as
 *  what did. */
export const SUPPRESSED_REASON_LABELS = {
  deduped_24h: '同专题 24 小时内已推送，合并',
  quiet_hours: '处于静默时段',
  production_mode: '简讯或转载，不单独推送',
}
export const CHANNEL_LABELS = { inapp: '站内', email: '邮件', webhook: 'Webhook' }
export const DIFF_KIND_LABELS = {
  numeric: '数值变化', modified: '表述修改', added: '新增段落', removed: '删除段落',
  not_a_change: '不是真实变化',
}

/** ``PARTIAL/timing_mismatch`` in Chinese, for either side of the matrix. */
export function labelText(kind, label) {
  if (!label) return '—'
  const [base, subtype] = label.split('/')
  const text = kind === 'verdict' ? (VERDICT_META[base]?.label || base)
    : kind === 'event' ? (RISK_LEVEL_LABELS[base] || base)
      : (DIFF_KIND_LABELS[base] || base)
  return subtype ? `${text}·${PARTIAL_SUBTYPE_LABELS[subtype] || subtype}` : text
}

export const PARTIAL_SUBTYPE_LABELS = {
  already_explained: '已解释',
  causality_added: '因果附加',
  timing_mismatch: '时间错位',
  magnitude_off: '数值偏差',
  overgeneralized: '范围夸大',
  wrong_entity: '主体错位',
  missing_qualifier: '遗漏限定',
}

/** 「无法核验」的子类（processor.unverifiable），与 api/labels.py 同一张表，由 test_labels 钉住。
 *  前三类是设计边界，后三类才是可以改进的部分。 */
export const UNVERIFIABLE_SUBTYPE_LABELS = {
  primary_disclosure: '出自权威披露',
  market_data: '行情数据',
  outside_disclosure: '披露范围之外',
  no_corpus: '未采集权威语料',
  not_specific: '披露未提及此细节',
  not_found: '披露中未检索到',
}

/** 研判方式。键是接口给出的中性码（I-06），与 `api/labels.py` 的 METHOD_LABELS 一致。 */
export const METHOD_LABELS = {
  numeric_rule: '规则比对（数值）',
  system_review: '系统研判',
  no_evidence: '检索无结果',
  no_corpus: '尚无权威语料',
}

/** Mirrors `api/labels.py`. `tests/test_labels.py` fails if the two drift —
 *  they had, badly: five issue codes and half the production modes showed as
 *  English on screen, and PRODUCTION_LABELS carried an `ORIGINAL` key that is
 *  not in the backend enum at all (the same stale key that was found and fixed
 *  in alerts.py's MODE_CREDIBILITY, still sitting here). */
export const ISSUE_LABELS = {
  FUND_RAISE: '募资',
  VALUATION: '估值',
  TECH_LEVEL: '技术水平',
  YIELD_CAPACITY: '良率与产能',
  CUSTOMER: '客户',
  SUPPLY_CHAIN: '供应链',
  LOCALIZATION: '国产化',
  EXPORT_CTRL: '出口管制',
  CYCLE_PRICE: '周期与价格',
  COMPETITION: '竞争',
  SHAREHOLDER: '股东',
  RELATED_PARTY: '关联交易',
  FINANCIAL: '财务',
  IP_PATENT: '知识产权',
  EXEC: '高管',
  STOCK_PRICE: '股价',
  LOCKUP: '限售与解禁',
  FUND_USE: '募资用途',
  DISCLOSURE: '信息披露',
  AUDIT_STAGE: '审核进程',
  DRUG_DEVELOPMENT: '药物研发',
  CYBERSECURITY: '数据安全',
  INDUSTRY_POLICY: '行业政策',
  INDUSTRY_TREND: '行业景气与产业发展',
  BUSINESS_EXPANSION: '业务拓展',
  STRATEGY_TRANSFORM: '战略调整与转型',
}
export const issueLabel = (code) => ISSUE_LABELS[code] || code

/** Alert and timeline levels are lower-case in the analytical tables, while the
 *  event ledger keeps its own upper-case levels; the two are read separately on
 *  purpose rather than normalised into one map here. */
/* 标签从「高／中／低」改成三个完整的词，有两个理由。一是规格要求状态标签必须自带
   文字（「不得只用颜色表示状态」），而单字标签在一列色块旁边仍然要读者先知道这一列
   讲的是风险；二是此前这里写「高」而 RISK_LEVEL_LABELS 写「高风险」——同一个量两个
   名字，正是上一轮专门收敛过的那类缺陷。low 叫「一般关注」而不是「低风险」：它的
   作用是排在后面处理，说成「低风险」会被读成「已经判断没风险」。 */
export const LEVEL_META = {
  high: { label: RISK_LEVEL_LABELS.high, color: 'var(--status-critical)' },
  medium: { label: RISK_LEVEL_LABELS.medium, color: 'var(--status-warning)' },
  low: { label: RISK_LEVEL_LABELS.low, color: 'var(--status-neutral)' },
}
/* 大小写不敏感：`event_records` 存 medium，`issue_ledger` 存 MEDIUM，同一个
   等级在两张表里两种写法，页面上不该因此一个显示「中」一个显示 MEDIUM。 */
export const levelLabel = (value) =>
  LEVEL_META[String(value ?? '').toLowerCase()]?.label || value || '—'

/** 命题输出要求里的「事件类型」。此前详情页与报告页直接显示枚举码。 */
export const EVENT_TYPE_LABELS = {
  REGULATORY_COMPLIANCE: '监管与合规',
  LITIGATION_ARBITRATION: '诉讼与仲裁',
  FINANCIAL_AUDIT: '财务与审计',
  IPO_REFINANCING: '发行与再融资',
  EQUITY_CONTROL: '股权与控制权',
  EXECUTIVE_PERSONNEL: '高管与人事',
  CUSTOMER_SUPPLIER_OPERATIONS: '客户、供应商与经营',
  PRODUCT_TECH_IP: '产品、技术与知识产权',
  SAFETY_QUALITY_RECALL: '安全、质量与召回',
  M_AND_A_ASSET_TRANSACTION: '并购与资产交易',
  ESG_REPUTATION: 'ESG 与声誉',
  RESPONSE_CLARIFICATION: '回应与澄清',
}

export const eventTypeLabel = (code) => EVENT_TYPE_LABELS[code] || code

/** 命题七项输出之三「影响对象」。同上。 */
export const IMPACT_OBJECT_LABELS = {
  operations: '经营运作',
  financial_performance: '财务表现',
  technology_product: '技术与产品',
  capacity_supply: '产能与供给',
  governance_internal_control: '治理与内控',
  shareholders_control: '股东与控制权',
  executives_core_staff: '高管与核心人员',
  employees: '员工',
  subsidiaries_related_parties: '子公司与关联方',
  customers: '客户',
  suppliers: '供应商',
  partners: '合作伙伴',
  competitors: '竞争对手',
  investors_stock_price: '投资者与股价',
  creditors_financing: '债权人与融资',
  regulatory_compliance: '监管与合规',
  reputation_brand: '声誉与品牌',
  industry: '行业',
  supply_chain: '供应链',
  regional_economy: '区域经济',
}

export const impactObjectLabel = (code) => IMPACT_OBJECT_LABELS[code] || code

/** 稿件语气。与影响方向是两回事，允许不一致——正因如此它有自己的一套标签，
 *  在页面上也和影响方向并排显示，而不是二选一。 */
export const ARTICLE_SENTIMENT_LABELS = {
  positive: '正面',
  neutral: '中性',
  negative: '负面',
}
export const sentimentLabel = (code) => ARTICLE_SENTIMENT_LABELS[code] || code || '—'

/** 影响方向与量级——七项输出之四的两个分量。 */
// 主张性质：判据见 processor/pipeline.py 的 CLAIM_TYPE_GUIDE。一条被标成「传言」的话
// 和一条被标成「事实」的话并列出现时，读者只能按标签取信，所以标签必须有中文名，
// 且悬停能看到它凭什么。
export const CLAIM_TYPE_LABELS = {
  fact: '事实陈述', inference: '推断', rumor: '传言', opinion: '观点',
}
export const CLAIM_TYPE_HINTS = {
  fact: '原文以确定语气陈述、可与权威文件核对的内容',
  inference: '由已知事实推出的结论、测算或预期，成立取决于一段推理',
  rumor: '来源不具名或未经证实的说法，原文自己也没当作已确认的事',
  opinion: '评价、判断或立场表达，没有可核对的内容',
}
export const claimTypeLabel = (code) => CLAIM_TYPE_LABELS[code] || code

// uncertain 一直没有中文名，而库里有 18 条——它在规则路径不可达，只有模型路径能给。
// moderate 从「中等」订正为「一般」，与总体方案 §8.4 和后端 labels 一致。
export const IMPACT_DIRECTION_LABELS = {
  positive: '正面', negative: '负面', neutral: '中性', uncertain: '待定',
}
export const IMPACT_MAGNITUDE_LABELS = {
  major: '重大', moderate: '一般', minor: '轻微',
}
// 「凭什么算重大」的受控取值：把一段散文变成一个可核对的短标签。
export const MAGNITUDE_BASIS_LABELS = {
  scale_threshold: '达到 10% / 单独披露门槛',
  entity_continuity: '触及主体资格或持续经营',
  forward_commitment: '改变跨期承诺',
  single_scope: '限于单个项目或报告期',
  routine_procedural: '程序性或例行事项',
}
export const magnitudeBasisLabel = (code) => MAGNITUDE_BASIS_LABELS[code] || code
// 风险分七项加减的项名。分数一直以一个数字示人，旁边的可信度却从第一天起就展示
// 它的四项加权贡献。
export const RISK_PART_LABELS = {
  base: '基线分',
  eventhood: '构成事件',
  direction: '影响方向为负面',
  magnitude: '影响强度',
  sensitive_type: '敏感事件类型',
  sensitive_issue: '敏感专题',
  secondhand: '二手稿件（不计独立信源）',
  capped: '分数封顶',
}
export const riskPartLabel = (key) => RISK_PART_LABELS[key] || key
// 复核原因此前直接以英文码 join('、') 打给读者。
export const REVIEW_REASON_LABELS = {
  low_confidence: '抽取置信度偏低',
  no_claims: '没有可定位的原子主张',
  unsupported_claim: '有主张无法在原文中定位',
  unresolved_elements: '有要素未能归一',
  issue_out_of_scope: '专题不适用于该公司所属行业，已剔除，待确认归属',
}
/** 来源层级的中文简称（I-06）：S0–S3 是内部分级码，读者读不出它说的是什么。 */
export const SOURCE_TIER_LABELS = { S0: '权威披露', S1: '官方发布', S2: '独立媒体', S3: '转载与线索' }
export const tierLabel = (tier) => SOURCE_TIER_LABELS[tier] || '其他'
export const reviewReasonLabel = (code) => REVIEW_REASON_LABELS[code] || code
export const impactText = (direction, magnitude) =>
  [IMPACT_DIRECTION_LABELS[direction] || direction || '—',
   IMPACT_MAGNITUDE_LABELS[magnitude] || magnitude].filter(Boolean).join(' · ')

export const CREDIBILITY_LABELS = { high: '可信度高', medium: '可信度中', low: '可信度低' }
/* 同一个量的短写法。表格列里「可信度」已经在表头上了，格子里再写一遍是浪费一列
   宽度；但它必须和长写法同源——风险事项库此前自己抄了一份 { high: '高' }，研判
   档案则干脆把后端的 medium 原样打给读者，一个量于是有了三种说法。 */
/* 写成多行不是排版偏好：test_labels 用 `= {(.*?)\n}` 从这个文件里把表读出来，
   一行写完的表没有结束的那个换行加右括号，正则会一直吞到下一张表的末尾。 */
export const CREDIBILITY_SHORT = {
  high: '高', medium: '中', low: '低',
}

/** How the credibility score is put together, so the page can show the formula
 *  next to the number instead of asserting it. */
export const CREDIBILITY_PARTS = [
  { key: 'verdict', label: '主张核验', weight: 0.4 },
  { key: 'source', label: '来源等级', weight: 0.3 },
  { key: 'independence', label: '独立信源', weight: 0.15 },
  { key: 'production', label: '生产方式', weight: 0.15 },
]

export const EDGE_KIND_LABELS = {
  FROM_DISCLOSURE: '源于披露', REPOST: '转载', REWRITE: '改写', RECURRING: '同源重复',
  FOLLOW_UP: '后续报道', RESPONSE: '公司回应', CLARIFICATION: '澄清辟谣',
  CORRECTION: '更正撤稿', CONFLICT: '口径冲突',
}
export const edgeKindLabel = (kind) => EDGE_KIND_LABELS[kind] || kind

/** 因果关系六类，对应 `processor/causal_link.py` 的 CAUSAL_KINDS。与上面九种传播
 *  边是两层：那一层回答「谁抄了谁、谁核了谁」，这一层回答「后一件事为什么发生」。
 *  CONTEXT_ONLY 是这一层的拒绝票，接口不会把它发到页面上——它留在表里当度量，
 *  一条被判为「只是共现」的候选不是脉络。 */
export const RELATION_LABELS = {
  TRIGGERS: '触发', RESPONDS_TO: '应答', ESCALATES: '升级',
  RESOLVES: '化解', REVISES: '修订', CONTEXT_ONLY: '仅背景相关',
}
export const relationLabel = (relation) => RELATION_LABELS[relation] || relation

export const CAUSAL_METHOD_LABELS = {
  rule_inquiry_alignment: '问询回复与招股书修订对齐',
  rule_institutional_sequence: '审核阶段次序',
  system_review: '系统研判，两端引证已核',
  system_review_unquoted: '系统研判，引证未能定位',
}
export const causalMethodLabel = (method) => CAUSAL_METHOD_LABELS[method] || method

export const FROM_KIND_LABELS = { disclosure: '披露文件', media: '媒体报道' }

/** 生产方式八类。这是任务三辨伪的结论本身，不该以英文示人。 */
export const PRODUCTION_LABELS = {
  ORIGINAL_INVESTIGATION: '原创调查',
  INTERPRETATION: '解读评论',
  REPOST: '转载',
  REWRITE: '改写',
  AI_GENERATED: '疑似机器生成',
  SPONSORED: '疑似宣发',
  UGC_EMOTIONAL: '情绪化 UGC',
  BRIEF: '简讯',
}

/** 时间线上这一条是按什么时间摆的。
 *
 *  原来是个三元表达式，只分「事件时间」和「发布时间」——于是 `fetched_at` 被
 *  印成了「发布时间」。**那不是漏了标签，是标错了**：6 条事件按抓取时间定位，
 *  页面却声称它们有发布时间。一个做脉络追踪的平台，把爬虫跑过的时刻说成
 *  稿件发布的时刻，比留空更糟。
 *
 *  抓取时间是取不到前两者时的兜底，措辞里就说明它是兜底。 */
export const TIME_BASIS_LABELS = {
  event_time: '事件时间',
  published_at: '发布时间',
  fetched_at: '抓取时间（兜底）',
}
export const basisLabel = (value) => TIME_BASIS_LABELS[value] || value || '—'

export function formatDate(value) {
  if (!value) return '—'
  return String(value).slice(0, 10)
}

/** 日期加时分。**只给审计页用**——那一页的全部意义是「谁在什么时候做了什么」，
 *  而 `formatDate` 截到日：同一天的三行读起来一模一样，既分不开也排不出先后。
 *
 *  别的页面继续用 `formatDate`：一条事件的发布日、一次披露的变更日，时分是噪音。 */
/** 计划/截止日期（I-04）：报道发布时尚在未来的事件时间。区间整段写出——
 *  「2027 年全年」截成 2027-01-01 会读成一个具体日子。 */
export function targetText(value) {
  if (!value) return ''
  const [start, end] = String(value).split('/')
  return `计划/截止 ${end && end !== start ? `${start} 至 ${end}` : start.slice(0, 10)}`
}

export function formatDateTime(value) {
  if (!value) return '—'
  const text = String(value)
  const time = text.slice(11, 16)
  return time ? `${text.slice(0, 10)} ${time}` : text.slice(0, 10)
}
