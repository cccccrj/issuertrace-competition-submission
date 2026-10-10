<script setup>
/**
 * 检索结果的分组渲染，顶栏下拉与 /search 结果页共用同一份。
 *
 * 共用的是「这一组叫什么、一条长什么样、点了去哪」这三件事。写两份的下场是
 * 下拉里点进事件详情、结果页里点进事件列表，而没人说得清哪个才是对的。
 *
 * 组之间不排序：结构化那六组是子串命中、没有分数，披露原文是 bm25，把两者折算
 * 成一个可比的数就是编数字。顺序由接口固定给出，这里照单渲染。
 */
import { RouterLink } from 'vue-router'

import { companyLabel, formatDate, issueLabel, levelLabel } from '../api.js'

const props = defineProps({
  groups: { type: Array, default: () => [] },
  // 下拉里点一条就该关掉浮层；结果页不需要。
  dense: { type: Boolean, default: false },
})
const emit = defineEmits(['picked'])

/** 一条结果点下去该到哪里。有详情页的进详情页，没有的回到它所属的列表页。 */
function target(kind, item, more) {
  if (kind === 'event') return `/events/${item.event_id}`
  if (kind === 'claim') return `/claims?search=${encodeURIComponent(item.text.slice(0, 20))}`
  if (kind === 'issue') return `/issues/${item.issue_id}`
  if (kind === 'alert') return `/alerts?issue=${item.issue_code || ''}`
  if (kind === 'diff') return `/diffs?search=${encodeURIComponent((item.right_text || '').slice(0, 20))}`
  if (kind === 'document') return `/ingestion?search=${encodeURIComponent(item.title || '')}`
  return more ? `${more.path}` : ''
}

function headline(kind, item) {
  if (kind === 'event') return item.summary
  if (kind === 'claim') return item.text
  if (kind === 'issue') return `${issueLabel(item.issue_code)}（${companyLabel(item.company_id)}）`
  if (kind === 'alert') return item.headline
  if (kind === 'diff') return item.right_text || item.left_text
  if (kind === 'document') return item.title
  return item.text
}

function note(kind, item) {
  if (kind === 'event') {
    return [companyLabel(item.company_id), formatDate(item.event_time || item.published_at),
            levelLabel(item.risk_level)].filter(Boolean).join(' · ')
  }
  if (kind === 'claim') return [companyLabel(item.company_id), issueLabel(item.issue_code)].join(' · ')
  if (kind === 'issue') return `${item.event_count} 个事件 · 独立信源 ${item.independent_source_count}`
  if (kind === 'alert') return [companyLabel(item.company_id), levelLabel(item.risk_level)].join(' · ')
  if (kind === 'diff') return [companyLabel(item.company_id), item.section, item.metric].filter(Boolean).join(' · ')
  if (kind === 'document') return [companyLabel(item.company_id), formatDate(item.published_at)].filter(Boolean).join(' · ')
  // 披露原文：文件名 + 节 + 页码，这三样才是能回去核对的地址。
  return [companyLabel(item.company_id), item.title, item.section,
          item.page ? `第${item.page}页` : ''].filter(Boolean).join(' · ')
}

const filled = (group) => group.items.length > 0
</script>

<template>
  <div class="results">
    <!-- 类名带前缀。此前是 `:class="group.kind"`，而 kind 里有一个 `evidence`
         （披露原文组），正好撞上 styles.css 的全局 .evidence：那一组二十条检索
         链接因此被整体套上引文的左线与底色——它是一列命中，不是一段引文。
         前缀让 kind 再也撞不到任何全局类，而不只是躲开这一个。 -->
    <section v-for="group in props.groups.filter(filled)" :key="group.kind"
             class="group" :class="`kind-${group.kind}`">
      <header>
        <span class="name">{{ group.label }}</span>
        <!-- 披露原文没有总数：数 FTS 命中要再扫一遍倒排，而那个数读者也翻不到。
             这里写「前 N 条」而不是摆一个伪造的总数。 -->
        <span class="muted small">{{ group.total === null
          ? `前 ${group.items.length} 条` : `共 ${group.total} 条` }}</span>
      </header>
      <ul>
        <li v-for="(item, index) in group.items" :key="index">
          <RouterLink :to="target(group.kind, item, group.more)" :title="headline(group.kind, item)"
                      @click="emit('picked')">
            {{ headline(group.kind, item) }}
          </RouterLink>
          <div class="muted small">{{ note(group.kind, item) }}</div>
        </li>
      </ul>
      <RouterLink v-if="group.more && group.total > group.items.length" class="small more"
                  :to="{ path: group.more.path, query: group.more.query }" @click="emit('picked')">
        在该页查看全部 {{ group.total }} 条 →
      </RouterLink>
    </section>
    <!-- `groups.length` 与「有没有命中」是两件事：一个字都没输时 groups 是空数组，
         查了但一条没中时它是七个各自为空的组。此前只看后半句，于是刚打开检索页、
         什么都还没查，页面就先说「没有匹配的结果」——没查过的东西说不上有没有。 -->
    <p v-if="props.groups.length && !props.groups.some(filled)" class="muted small empty">
      没有匹配的结果。
    </p>
  </div>
</template>

<style scoped>
.group { padding: 8px 0; border-bottom: 1px solid var(--border); }
.group:last-of-type { border-bottom: 0; }
header { display: flex; gap: 8px; align-items: baseline; margin-bottom: 4px; }
.name { font-weight: 600; font-size: var(--fs-aux); }
ul { list-style: none; margin: 0; padding: 0; }
li { padding: 3px 0; max-width: var(--measure); }
/* 前六组的条目是标题（事件摘要、主张、公告名），一行足够辨认，截一行；
   披露原文那一组是整段正文，截两行。限宽之后如果一律截两行，原本一行的条目
   会变成两行——实测检索结果页因此从 5,368px 涨到 6,607px，正好是反效果。
   全文在 title 属性里，点进去也能看到。 */
li a { display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;
  overflow: hidden; }
.group.kind-evidence li a { -webkit-line-clamp: 2; }
.more { display: inline-block; margin-top: 4px; }
.empty { padding: 12px 0; }
</style>
