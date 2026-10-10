<script setup>
import { ref } from 'vue'
import { DIFF_KIND_LABELS } from '../api.js'

defineProps({ inquiry: { type: Object, required: true }, compact: { type: Boolean, default: false } })
const open = ref(false)
</script>

<template>
  <!-- 问 → 答 → 改：三份不同的文件，串起来才撑得住「已经解释过」这句话。 -->
  <section class="chain">
    <header>
      <span class="tag">问询对齐</span>
      <b>{{ { first: '首轮问询', second: '第二轮问询', third: '第三轮问询',
              implementation: '落实函' }[inquiry.round] || '问询' }}第 {{ inquiry.number }} 问 ·
        关于{{ inquiry.topic }}</b>
      <span class="muted small">《{{ inquiry.document_title }}》第 {{ inquiry.question_page }} 页提出，
        第 {{ inquiry.answer_page ?? inquiry.question_page }} 页起回复</span>
    </header>

    <ol class="steps">
      <li>
        <span class="step">问</span>
        <div>
          <div class="text" :class="{ clipped: !open }">{{ inquiry.question_text }}</div>
          <button class="link" @click="open = !open">{{ open ? '收起问题原文' : '展开问题原文' }}</button>
        </div>
      </li>
      <li v-if="inquiry.changes.length">
        <span class="step">答</span>
        <div>
          <div class="muted small">回复第 {{ inquiry.changes[0].response_page }} 页</div>
          <div class="text">{{ inquiry.changes[0].response_excerpt }}</div>
        </div>
      </li>
      <li v-if="inquiry.changes.length">
        <span class="step">改</span>
        <div>
          <div class="muted small">这一问之后，招股书里跟着变了 {{ inquiry.changes.length }} 处</div>
          <ul class="changes">
            <li v-for="change in inquiry.changes.slice(0, compact ? 2 : 5)" :key="change.diff_id">
              <span class="muted small">{{ change.section }} · {{ change.pair_key }} ·
                第 {{ change.right_page }} 页 ·
                {{ DIFF_KIND_LABELS[change.diff_kind] || change.diff_kind }}</span>
              <div v-if="change.old_value" class="mono small">
                {{ change.old_value }} → {{ change.new_value }}
              </div>
              <div v-else class="text small">{{ (change.right_text || '').slice(0, 140) }}</div>
              <!-- 匹配是靠共享的生僻词算出来的，把词摆出来，读者才能自己判断这处修改算不算数。 -->
              <div class="muted small">共同词：{{ (change.shared_terms || []).slice(0, 8).join('、') }}
                （{{ (change.match_score * 100).toFixed(1) }}%）</div>
            </li>
          </ul>
        </div>
      </li>
      <li v-else>
        <span class="step">改</span>
        <div class="muted small">未找到与该问询对应的招股书修改：问询已回复，但披露文本未随之变更。</div>
      </li>
    </ol>
  </section>
</template>

<style scoped>
/* 问询链嵌在卡片里。此前它取 --surface-1，也就是和外面那张卡同色——一张白卡
   套一张白卡，只剩一条边框在说它是另一层东西。改成内嵌底。
   不给蓝色左线：蓝线的意思是「带页码的逐字原文」，而这里是一个装着问、答、改
   三段的结构，原文在它里面的 .text 上。 */
.chain { border: 1px solid var(--border); border-radius: var(--radius); padding: 11px 13px;
  margin: 10px 0; background: var(--surface-2); }
.chain header { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; margin-bottom: 8px; }
.steps { list-style: none; margin: 0; padding: 0; }
.steps > li { display: flex; gap: 10px; padding: 7px 0; border-top: 1px solid var(--gridline); }
.steps > li:first-child { border-top: none; }
.step { flex: none; width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center;
  font-size: var(--fs-note); background: var(--surface-2, var(--hover-wash)); color: var(--text-secondary); }
.text { font-size: var(--fs-body); line-height: 1.7; }
.text.clipped { display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; }
.changes { list-style: none; margin: 4px 0 0; padding: 0; }
.changes > li { padding: 4px 0; border-top: 1px dashed var(--gridline); }
.changes > li:first-child { border-top: none; }
button.link { background: none; border: none; padding: 0; color: var(--text-secondary);
  text-decoration: underline; cursor: pointer; font: inherit; font-size: var(--fs-note); }
</style>
