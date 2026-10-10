<script setup>
/**
 * 使用指南 —— 给第一次打开这个平台的人。
 *
 * 驾驶舱回答的是「库里有多少东西」：四个数字、六张任务卡、两张趋势图。它答得很好，
 * 但它不回答「这东西是什么」和「我该先点哪」。侧栏二十一个链接分七组，组名是命题的
 * 六项任务——对熟悉命题的人是目录，对第一次来的人是二十一个陌生名词。
 *
 * 这一页不放在阅读路径上。2026-09-06 各页的说明性文字被整体撤下（净减 2,592 字），
 * 理由是它横在读者和数据中间；那个判断是对的。撤下来的东西缺的不是位置，是一个家：
 * 一个入口、读一次、之后再也不必经过。
 *
 * 「跟着走一遍」的六步来自 /api/guide，事件 id 由接口当场挑，不写死在这里。写死的
 * id 会随下一次重跑处理层失效，而这是第一次来的人唯一会把每条链接都点一遍的页面。
 */
import { onMounted, ref } from 'vue'

import {
  CREDIBILITY_PARTS, ISSUE_STATUS_LABELS, METHOD_LABELS, PARTIAL_SUBTYPE_LABELS,
  PRODUCTION_LABELS, RISK_LEVEL_LABELS, VERDICT_META,
  api, companyLabel, issueLabel, levelLabel,
} from '../api.js'
import {
  CREDIBILITY_PART_GLOSS, ISSUE_STATUS_GLOSS, METHOD_GLOSS, PARTIAL_SUBTYPE_GLOSS,
  PRODUCTION_GLOSS, RISK_LEVEL_GLOSS, SOURCE_TIER_GLOSS, VERDICT_GLOSS,
} from '../glossary.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import DataTable from '../components/ui/DataTable.vue'
import PageHeader from '../components/ui/PageHeader.vue'

const guide = ref(null)
const error = ref(null)
const loading = ref(true)

onMounted(async () => {
  try {
    guide.value = await api.guide()
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
})

/** 三条按目的分岔的入口。第一条指向本页的六步，另外两条离开本页。 */
const ROUTES = [
  { who: '初次使用', want: '了解平台功能',
    go: '按上方六个步骤浏览，各步骤均基于同一条真实事件。', to: null },
  { who: '日常工作', want: '分析师、核查负责人',
    go: '从风险预警中心进入，查看当日待处理事项及下一步操作。', to: '/' },
  { who: '结果验证', want: '了解结论的可信依据',
    go: '研判质量评测页是官方标注数据上的复现式评测，复核任务是人工判定，审计日志记着每次处理与每次人工操作。',
    to: '/benchmark' },
]

/** 平台自己的名词。受控词表里那些码（事件类型、议题码、影响对象）另有去处——
 *  主数据与词表页逐条给了定义与判定规则，这里不抄第二份。 */
const TERMS = [
  { term: '文档版本', gloss: '不可变的原文加它的 SHA-256。下游一切结论都可以删掉重算，它不可以被覆盖。', to: '/ingestion' },
  { term: '事件', gloss: '从一篇文档里抽出的、与受监控主体相关且构成事件的一条记录。', to: '/events' },
  { term: '要素', gloss: '事件的结构化字段：主体、时间、金额、比例、人物、机构、产品。每条都记着原文、归一值与归一依据。', to: '/elements' },
  { term: '原子主张', gloss: '事件拆出的、能单独判真伪的一句话。定位不回原文的主张会被丢弃。', to: '/claims' },
  { term: '裁决', gloss: '对一条主张的四值判断：支持、部分支持、冲突、无法核验。除「无法核验」外每条都带证据页码。', to: '/claims' },
  { term: '无法核验', gloss: '证据侧的缺口，不是核验失败——该主体还没有可比对的权威披露语料时就是这个值。', to: '/claims' },
  { term: '同源簇', gloss: '近重复归并后的一组文档版本，让同一篇稿的多处出现只算一次。', to: '/duplicates' },
  { term: '独立信源', gloss: '剔除转载与改写之后剩下的声音数。它是可信度评估的分母，不是文章数。', to: '/propagation' },
  { term: '专题', gloss: '（公司，专题码）二元组，平台的核心业务对象，带自己的状态机。注意它不是工单。', to: '/issues' },
  { term: '披露阶段系数', gloss: '从公司自己的申报链推导出来的倍数，用来修正风险分，不写死日期。', to: '/timeline' },
  { term: '风险事项', gloss: '越过预警线的专题，按（公司，专题，7 天发布窗口）合并。同专题同窗口只出一条，其余并入事件数——所以事项数远少于事件数。', to: '/risks' },
  { term: '复核任务', gloss: '关于裁决的第二层人工事实，不是对裁决的编辑——判定不会改写处理层的结论。', to: '/review' },
  { term: '发布方', gloss: '这条消息由几家发出。与「独立信源」不是一个数——十家转载同一篇稿，发布方是十家，独立信源是一条。', to: '/duplicates' },
]

/** 上面十三条是平台自己的名词；下面这些是页面上那些码的大白话，来自 glossary.js
 *  ——徽章悬停用的是同一份文本。悬停够不着触屏、键盘与读屏，所以这张表不是备份，
 *  是保底：每个能悬停的词，在这里都查得到。
 *
 *  受控词表里那些码（事件类型、议题码、影响对象）仍然不在这里——主数据与词表页
 *  逐条给了定义与判定规则，这里不抄第二份。 */
const CODE_SECTIONS = [
  { title: '裁决的四个值', to: '/claims', gloss: VERDICT_GLOSS, labels: (c) => VERDICT_META[c]?.label || c },
  { title: '「部分支持」的七种出入', to: '/claims', gloss: PARTIAL_SUBTYPE_GLOSS,
    labels: (c) => PARTIAL_SUBTYPE_LABELS[c] || c },
  { title: '裁决是怎么得出的', to: '/claims', gloss: METHOD_GLOSS, labels: (c) => METHOD_LABELS[c] || c },
  { title: '可信度的四项', to: '/risks', gloss: CREDIBILITY_PART_GLOSS,
    labels: (c) => CREDIBILITY_PARTS.find((p) => p.key === c)?.label || c },
  { title: '来源层级', to: '/ingestion', gloss: SOURCE_TIER_GLOSS, labels: (c) => c },
  { title: '风险等级', to: '/risks', gloss: RISK_LEVEL_GLOSS, labels: (c) => RISK_LEVEL_LABELS[c] || c },
  { title: '专题的五个状态', to: '/issues', gloss: ISSUE_STATUS_GLOSS,
    labels: (c) => ISSUE_STATUS_LABELS[c] || c },
  { title: '稿件的生产方式', to: '/profiling', gloss: PRODUCTION_GLOSS,
    labels: (c) => PRODUCTION_LABELS[c] || c },
].map((section) => ({
  ...section,
  rows: Object.entries(section.gloss).map(([code, text]) => ({ term: section.labels(code), gloss: text })),
}))

const CODE_COLUMNS = [
  { key: 'term', title: '取值', slot: 'term', nowrap: true, width: '150px' },
  { key: 'gloss', title: '是什么', slot: 'gloss' },
]

const TERM_COLUMNS = [
  { key: 'term', title: '名词', slot: 'term', nowrap: true, width: '110px' },
  { key: 'gloss', title: '是什么', slot: 'gloss' },
  { key: 'to', title: '', slot: 'go', nowrap: true, width: '64px' },
]
</script>

<template>
  <PageHeader title="使用指南" note="平台功能概述与入门指引。" />

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <section class="card">
      <h2>平台简介</h2>
      <p class="lede">
        将<b>媒体报道</b>与<b>公司披露</b>逐条核验、并按<b>专题</b>持续追踪的核查协同平台。
      </p>
      <p class="prose">
        本平台并非财经新闻爬虫或自动摘要工具，每一条结论均可追溯至所依据文件的具体页码。
      </p>
      <dl v-if="guide" class="detail-grid">
        <dt>受监控主体</dt>
        <dd>{{ guide.boundary.companies.map(companyLabel).join(' · ') }}</dd>
        <dt>已入库事件</dt>
        <dd class="mono">{{ guide.boundary.events.toLocaleString() }}</dd>
        <dt v-if="guide.boundary.earliest">语料时间</dt>
        <dd v-if="guide.boundary.earliest">
          {{ guide.boundary.earliest }} 至 {{ guide.boundary.latest }}
          <span v-if="guide.boundary.latest_is_future" class="muted">
            （后者晚于今天，是发布时间的解析异常，不是预告）
          </span>
        </dd>
      </dl>
      <Callout tone="warn" title="边界">
        风险等级只用于内部排序、展示与预警，<b>不替代监管认定或人工专业判断</b>。
        平台记录人工判定，但不改写处理层产出的结论。
      </Callout>
    </section>

    <h2 class="section">跟着走一遍：命题的六项主要任务</h2>
    <!-- 这一段此前的举证责任在侧栏——七个分组逐字写着命题的六项任务，「读侧栏即读
         任务表」。侧栏改成按业务任务组织之后，那份对照没有取消，搬到了这里：六张卡
         就是六项任务，每一张都点得进它在平台上的落点。views/guide.test.js 钉着这
         六个名字和它们各自的入口。 -->
    <p class="prose intro">
      下面六张卡逐一对应命题要求的六项主要任务。每一张都能点进它在平台上的落点，
      可直接查看各项任务的实际运行数据。
    </p>
    <p class="prose intro" v-if="guide?.sample">
      下面六步落在同一条真实事件上：<b>{{ companyLabel(guide.sample.company_id) }}</b> ·
      {{ issueLabel(guide.sample.issue_code) }} · {{ levelLabel(guide.sample.risk_level) }}。
      该样例在打开本页时从 <b class="mono">{{ guide.candidates }}</b> 条六步齐全的候选事件中动态选取，
      处理层重新运行后会相应更新，不会产生失效链接。
    </p>
    <p class="prose intro" v-else>
      当前部署暂无六步齐全的样例事件，以下各步骤分别链接至对应页面。
    </p>

    <div class="steps">
      <component
        :is="step.available ? 'RouterLink' : 'div'"
        v-for="step in guide?.steps || []"
        :key="step.key"
        :to="step.available ? step.to : undefined"
        class="card step"
        :class="{ off: !step.available }"
      >
        <div class="step-head">
          <span class="ordinal">{{ step.ordinal }}</span>
          <span class="title">{{ step.title }}</span>
          <span v-if="step.available" class="go" aria-hidden="true">→</span>
        </div>
        <p class="does">{{ step.does }}</p>
        <p class="caution">{{ step.caution }}</p>
        <p v-if="!step.available" class="off-note">
          {{ step.unavailable_reason }}
          <RouterLink :to="step.to">仍可查看整页 →</RouterLink>
        </p>
      </component>
    </div>

    <h2 class="section">入门路径</h2>
    <div class="routes">
      <section v-for="row in ROUTES" :key="row.who" class="card route">
        <div class="who">{{ row.who }}<span class="want">{{ row.want }}</span></div>
        <p class="go-text">{{ row.go }}</p>
        <RouterLink v-if="row.to" :to="row.to">前往 →</RouterLink>
      </section>
    </div>

    <h2 class="section">名词</h2>
    <div class="card">
      <DataTable :columns="TERM_COLUMNS" :rows="TERMS" row-key="term">
        <template #term="{ row }"><b>{{ row.term }}</b></template>
        <template #gloss="{ row }">{{ row.gloss }}</template>
        <template #go="{ row }"><RouterLink :to="row.to">去看 →</RouterLink></template>
      </DataTable>
    </div>

    <h2 class="section">页面上那些码</h2>
    <p class="prose intro">
      以下释义与页面悬停提示一致，供触屏、键盘及读屏用户查阅。
    </p>
    <section v-for="section in CODE_SECTIONS" :key="section.title" class="card">
      <h2>{{ section.title }} <RouterLink class="go-page" :to="section.to">去看 →</RouterLink></h2>
      <DataTable :columns="CODE_COLUMNS" :rows="section.rows" row-key="term">
        <template #term="{ row }"><b>{{ row.term }}</b></template>
        <template #gloss="{ row }">{{ row.gloss }}</template>
      </DataTable>
    </section>
  </AsyncSection>
</template>

<style scoped>
.lede { font-size: var(--fs-title); font-weight: 600; margin: 0 0 var(--sp-2); max-width: var(--measure); }
.prose { max-width: var(--measure); margin: 0 0 var(--sp-3); color: var(--text-secondary); }
.intro { margin-bottom: var(--sp-3); }
.section { font-size: var(--fs-title); font-weight: 650; margin: var(--sp-5) 0 var(--sp-3); }
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 var(--sp-3); }
.steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: var(--sp-3); }
@media (min-width: 1440px) { .steps { grid-template-columns: repeat(3, 1fr); } }
.step { display: block; color: inherit; }
.steps > .card { margin-top: 0; margin-bottom: 0; }
.step:hover { text-decoration: none; border-color: var(--series-1); }
.step:hover .title { text-decoration: underline; }
/* 走不通的那一步保持可读，只是不再像个可点的东西——数得出来这条路径有六步，
   缺了哪一步本身就是信息。 */
.step.off { color: var(--text-muted); }
.step.off:hover { border-color: var(--border); }
.step-head { display: flex; align-items: baseline; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.ordinal {
  flex: none; width: 18px; height: 18px; border-radius: 50%; font-size: var(--fs-note);
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--surface-2); color: var(--text-secondary);
}
.title { font-weight: 600; font-size: var(--fs-body); }
.go { margin-left: auto; color: var(--text-muted); }
.does { margin: 0 0 var(--sp-2); font-size: var(--fs-aux); color: var(--text-secondary);
  line-height: var(--lh-body); }
.caution { margin: 0; font-size: var(--fs-note); color: var(--text-muted); line-height: var(--lh-body); }
.off-note { margin: var(--sp-2) 0 0; font-size: var(--fs-note); color: var(--text-muted); }
.routes { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr)); gap: var(--sp-3); }
.routes > .card { margin-top: 0; margin-bottom: 0; }
.go-page { font-weight: 400; font-size: var(--fs-note); margin-left: var(--sp-2); }
.route { display: flex; flex-direction: column; gap: var(--sp-2); }
.who { font-weight: 650; font-size: var(--fs-body); }
.want { color: var(--text-muted); font-size: var(--fs-note); font-weight: 400; margin-left: var(--sp-2); }
.go-text { margin: 0; flex: 1; font-size: var(--fs-aux); color: var(--text-secondary);
  line-height: var(--lh-body); }
</style>
