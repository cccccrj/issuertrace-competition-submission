/** 术语的大白话，一份，两个消费者。
 *
 * 反馈是「加点通俗解释的标签」。此前全站只有 `api.js` 的 CLAIM_TYPE_HINTS 是
 * 「标签 / 解释」成对的，而且只被一个 `:title=` 用过一次；其余二十多张 *_LABELS
 * 全是纯中文改名，于是裁决四值、S0–S3、议题状态这些词在页面上裸奔。
 *
 * **这里的文字是重写的，不是从 /api/vocabularies 抄的。** 那边的 `definition` 是
 * 写给模型的判据（处理层的承重文本），口吻是排他式的「不属本类」「才给」，每条
 * 八十到一百五十字，还带着字面 Markdown 星号。判据要精确，解释要好懂，两件事。
 *
 * 消费者有两个，共用这一份：
 *   1. 悬停——徽章与标签上的 `title`，零版面成本；
 *   2. /guide 的名词表——悬停够不着的地方（触屏、键盘、读屏）由它兜底。
 * 同一句话在两处各存一份，正是界面体检记录 §五 批评过的东西。
 *
 * 写法：一句「是什么」，必要时补一句「它不是什么」——后者往往才是读者真正会读错
 * 的地方。
 */

/** 裁决四值。全站出现最多的一组。 */
export const VERDICT_GLOSS = {
  SUPPORTED: '媒体这句话，在权威披露原文里找得到对应的表述。',
  PARTIAL: '大体属实，但有出入——时间、数值、范围或主体对不上，具体哪一种见子类。',
  CONFLICT: '披露原文说的与这句话相反，不是没找到，是找到了且不一致。',
  UNVERIFIABLE: '在已采集的披露语料里找不到能对应的段落。这是证据侧的缺口，不等于这句话是假的。',
}

/** 部分支持的七个子类——外行最猜不到的一组。 */
export const PARTIAL_SUBTYPE_GLOSS = {
  already_explained: '交易所问过、公司答过，答案有页码可查。处置上是降级，不是忽略。',
  causality_added: '事实对得上，但因果是媒体自己接的，披露原文没这么说。',
  timing_mismatch: '事情属实，时间点对不上。',
  magnitude_off: '事情属实，数字或比例与披露的对不上。',
  overgeneralized: '披露里是有限定的说法，到了媒体这里被说成普遍情况。',
  wrong_entity: '事情属实，但主体错了——张冠李戴到了另一家或另一个层级。',
  missing_qualifier: '披露原文带着前提或条件，媒体把这些前提去掉了。',
}

/** 裁决是怎么得出的。「没找到」与「没语料」是两回事。 */
export const METHOD_GLOSS = {
  numeric_rule: '数值直接比对得出。',
  system_review: '由系统比对媒体主张与披露原文后判定。',
  no_evidence: '检索过，但没有命中相关段落。',
  no_corpus: '这家主体还没有采集到权威披露语料，无从比对——是缺口，不是失败。',
}

/** 风险等级。它说的是「如果为真，影响多大」，与可信度是两个轴——
 *  一条只有单一转述来源的传闻可以同时是高风险和低可信。 */
export const RISK_LEVEL_GLOSS = {
  high: '如果属实，影响大。这说的是影响，不是这条消息有多可信——两者是分开的两个数。',
  medium: '如果属实，影响中等。',
  low: '如果属实，影响有限，排在后面处理。它不等于「已判断没有风险」。',
}

/** 可信度的四项权重。合成一个分数会藏起最该看的那类，所以四项分开列。 */
export const CREDIBILITY_PART_GLOSS = {
  verdict: '这条专题下的主张，有多少能在披露原文里对上。占 40%。',
  source: '消息来自哪一层来源——发布方本人、独立媒体，还是转载。占 30%。',
  independence: '剔除转载与改写之后，还剩几条互相独立的报道。占 15%。',
  production: '稿子是怎么生产的——原创调查、解读评论，还是简讯与转载。占 15%。',
}

/** 来源层级。权威定义在 config/sources.yaml，这里是它的大白话版。 */
export const SOURCE_TIER_GLOSS = {
  S0: '政府、监管机构、交易所与法院——法定信息披露的出处本身。',
  S1: '公司官网与法定信披镜像站，即发布方自己说的话。',
  S2: '独立媒体与专业数据库，不是发布方本人。',
  S3: '转载、社交媒体与论坛。同一条消息可能已经过了好几手。',
}

/** 议题状态。门限由处理层判定，这里说的是每个状态意味着什么。 */
export const ISSUE_STATUS_GLOSS = {
  emerging: '最近才第一次出现，还看不出走势。',
  escalating: '近期报道明显变多或新事实在增加，正在升温。',
  active: '持续有新事件进来，热度平稳。',
  cooling: '还在发生，但比之前明显少了。',
  dormant: '已经很久没有新事件，暂时沉寂。',
}

/** 稿件生产方式。判定为推断，不对外发布——这一点写在方案的红线里。 */
export const PRODUCTION_GLOSS = {
  ORIGINAL_INVESTIGATION: '记者自己采访、调查得来的内容。',
  INTERPRETATION: '在已有事实上做解读、评论或推演。',
  REPOST: '整篇照搬另一家的稿子。',
  REWRITE: '换了说法，但骨架来自别人的稿子。',
  AI_GENERATED: '疑似由程序批量生成的模板化稿件。',
  SPONSORED: '疑似宣发——通篇引用公司口径、无独立信源。这是推断，不是认定。',
  UGC_EMOTIONAL: '用户生成、情绪化表达为主。',
  BRIEF: '简讯，只有梗概，没有独立采写。',
}

/** 一个码 → 一句话。找不到时返回空串，调用点因此可以直接绑 `:title`。 */
const TABLES = [VERDICT_GLOSS, PARTIAL_SUBTYPE_GLOSS, METHOD_GLOSS,
                CREDIBILITY_PART_GLOSS, SOURCE_TIER_GLOSS, ISSUE_STATUS_GLOSS,
                PRODUCTION_GLOSS, RISK_LEVEL_GLOSS]

export function gloss(code) {
  if (!code) return ''
  for (const table of TABLES) if (table[code]) return table[code]
  return ''
}

/** 裁决带子类时（`PARTIAL/timing_mismatch`），两句都给。 */
export function verdictGloss(verdict, subtype) {
  const head = VERDICT_GLOSS[verdict] || ''
  const tail = subtype ? PARTIAL_SUBTYPE_GLOSS[subtype] || '' : ''
  return [head, tail].filter(Boolean).join('\n')
}
