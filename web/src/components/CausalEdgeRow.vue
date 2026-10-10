<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import { causalMethodLabel, formatDate, relationLabel } from '../api.js'

const props = defineProps({
  edge: { type: Object, required: true },
  // 折叠区里的行不展开引证：那一层是「还有这些」，不是「请逐条读」。
  quotes: { type: Boolean, default: true },
})

// 语料里 203 个文档版本没有标题（多为港交所与问询回复的分册），此前这里会渲染
// 成一个没有文字的链接。退到事件摘要，并说明退过——空链接比一句摘要更糟，
// 而把摘要冒充成标题也不诚实。
const untitled = computed(() => !String(props.edge.title || '').trim())
const label = computed(() => (untitled.value
  ? String(props.edge.summary || '').slice(0, 42) || '（无标题文档）'
  : props.edge.title))

const gapLabel = (hours) => {
  if (hours === null || hours === undefined) return '时间不详'
  const days = Math.round(Math.abs(hours) / 24)
  return days >= 1 ? `${days} 天` : `${Math.round(Math.abs(hours))} 小时`
}
</script>

<template>
  <!-- 判据在这里是两端各一句话，不是一个相似度分数——旧的「同议题，相似度 0.33」
       之所以取信不了人，正是因为它把计算过程当成了理由。
       这里写「引证」而不是「原文引证」：引文校验的范围是事件文本（标题 + 原子主张 +
       模型写的事件摘要），实测 8,259 句里只有 30.6% 能在标题或主张中找到，其余引的是
       模型对该文档的复述。措辞必须说到这一层，见 docs/因果边自查记录.md §四。 -->
  <article class="edge">
    <div class="edge-head">
      <span class="tag rule">{{ relationLabel(props.edge.relation) }}</span>
      <RouterLink :to="`/events/${props.edge.other_event_id}`">{{ label }}</RouterLink>
      <span v-if="untitled" class="muted small">该文档无标题，此处取事件摘要</span>
    </div>
    <div class="muted small">
      {{ formatDate(props.edge.published_at) }} · 相隔 {{ gapLabel(props.edge.gap_hours) }} ·
      {{ causalMethodLabel(props.edge.method) }}
    </div>
    <p class="small reason">{{ props.edge.reason }}</p>
    <details v-if="props.quotes && (props.edge.quote_from || props.edge.quote_to)">
      <summary class="small">引证</summary>
      <p v-if="props.edge.quote_from" class="small quote">
        <span class="muted">前｜</span>{{ props.edge.quote_from }}
      </p>
      <p v-if="props.edge.quote_to" class="small quote">
        <span class="muted">后｜</span>{{ props.edge.quote_to }}
      </p>
    </details>
  </article>
</template>

<style scoped>
.edge { border-left: 2px solid var(--border); padding-left: 10px; margin-bottom: 12px; }
.edge-head { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.reason { margin: 4px 0 0; }
summary { cursor: pointer; color: var(--text-muted); }
.quote { margin: 4px 0 0; padding-left: 8px; border-left: 2px solid var(--border); }
</style>
