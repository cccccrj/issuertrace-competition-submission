<script setup>
/**
 * 一条证据引证：《文件名》第 N 页 · 章节，加上被引用的原文。
 *
 * 建这个件的直接原因是「证据能加超链接吗——直接到原网页，对应那个『可核验』」。
 * 摸底之后发现不是漏了一处，是整条证据路径上都没有：四个后端接口都 JOIN 了
 * document_versions，却只取 title 和 document_type，独独没取 canonical_url。于是
 * 页面上那个书名号——全站最像链接的地方——恰恰不是链接。
 *
 * 最能说明问题的是预警抽屉：同一屏上「主张与裁决」的《自愿公告》是纯文本，往下
 * 十五行的「合并的事件」有「原文 →」外链。两种待遇隔着十五行。三处各写一遍就是
 * 这么来的，所以这次收成一个件。
 *
 * `#page=` 是这里唯一算得上聪明的一步：被引证的文档 100% 是 PDF 直链，而页码来自
 * processor/pipeline.py 的 `[PAGE {page.number + 1}]`——PyMuPDF 的物理页序，正是
 * `#page=N` 的语义。于是「可核验」从「我们告诉你在第 43 页」变成「点一下就在第
 * 43 页」。浏览器不支持时锚点被忽略，退回文件首页，不会坏。
 */
import { computed } from 'vue'

const props = defineProps({
  title: { type: String, default: '' },
  url: { type: String, default: '' },
  page: { type: [Number, String], default: null },
  section: { type: String, default: '' },
  /** 尾巴上的一句元信息，例如裁决方式。 */
  note: { type: String, default: '' },
  /** 被引用的原文。 */
  text: { type: String, default: '' },
})

/** 有页码就跳到那一页。锚点只对 PDF 有意义，但这里的证据全是 PDF；
 *  万一不是，浏览器会忽略它而不是报错。 */
const href = computed(() =>
  props.url ? (props.page ? `${props.url}#page=${props.page}` : props.url) : '')
</script>

<template>
  <div class="evidence">
    <div class="evidence-head">
      <template v-if="title">
        <a v-if="href" :href="href" target="_blank" rel="noopener">《{{ title }}》</a>
        <span v-else>《{{ title }}》</span>
      </template>
      <template v-if="page">第 {{ page }} 页</template>
      <template v-if="section"> · {{ section }}</template>
      <template v-if="note"> · {{ note }}</template>
      <slot name="head" />
    </div>
    <div v-if="text" class="evidence-text">{{ text }}</div>
    <slot />
  </div>
</template>

<style scoped>
/* .evidence / .evidence-head / .evidence-text 是 styles.css 的全局类（内嵌底 +
   2px 强调左线，「带页码的逐字原文」那个记号），这里不重复声明。 */
.evidence-head a { color: var(--series-1); }
</style>
