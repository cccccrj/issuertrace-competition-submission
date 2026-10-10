<script setup>
/** 研判助手面板：问一句，看它一步步查、每句话凭什么。
 *
 *  三件事必须在页面上看得见，否则「智能」只是一个黑箱：
 *  - 每句回答后面跟着出处编号，点得开（站内对象跳转，披露段落展开原文与页码）；
 *  - 查证过程**边查边显示**——第几步、用了哪个工具、查的是什么、为什么查（模型每一步
 *    写给人看的一句意图）、查到了什么。换成更准但更慢的模型之后，等的人看得见它在做什么；
 *  - 服务不可用、查证没做完、有句子因为没有出处被删掉，都明说。
 *
 *  助手只解释平台已有的结论，不改判。规则在 api/assistant.py。 */
import { computed, ref, watch } from 'vue'
import { api } from '../api.js'
import Callout from './ui/Callout.vue'

const props = defineProps({
  scope: { type: String, required: true },      // 'risk' | 'event'
  objectId: { type: String, required: true },
})

const EXAMPLES = {
  risk: ['这条为什么是这个风险分？', '这条消息有没有权威来源支持？', '招股书里有没有提到相关风险？'],
  event: ['这件事对公司的影响依据是什么？', '公司对这件事有没有发过公告回应？', '这条报道里的说法核实了吗？'],
}
const KIND_LABELS = { risk: '风险事项', event: '事件', claim: '主张核验', issue: '专题', paragraph: '披露原文' }
const MAX_CHARS = 300

const question = ref('')
const loading = ref(false)
const result = ref(null)
const error = ref('')

// 换了一条事项，上一条的回答不该还挂在这里。
watch(() => props.objectId, () => { result.value = null; error.value = ''; live.value = [] })

const examples = computed(() => EXAMPLES[props.scope] || [])
// 边查边显示：流里每来一步就追加一行；结果到了以后以结果里的为准（两者内容相同）。
const live = ref([])
const assistantSteps = computed(() => (result.value?.steps?.length ? result.value.steps : live.value))

async function ask(text) {
  const asked = (text ?? question.value).trim()
  if (!asked || loading.value) return
  question.value = asked
  loading.value = true
  error.value = ''
  result.value = null
  live.value = []
  try {
    result.value = await api.askAssistantStream(props.scope, props.objectId, asked,
                                                (step) => { live.value = [...live.value, step] })
  } catch (failure) {
    error.value = failure.message || '提问失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

function isInternal(link) {
  return typeof link === 'string' && link.startsWith('/')
}
</script>

<template>
  <section id="assistant" class="card assistant" aria-labelledby="assistant-title">
    <div class="head">
      <h2 id="assistant-title" class="section">研判助手</h2>
      <span class="muted small">只解释平台已有的结论，不改判；每句话都带出处。</span>
    </div>

    <form class="ask" @submit.prevent="ask()">
      <input v-model="question" type="text" :maxlength="MAX_CHARS" aria-label="向研判助手提问"
             placeholder="例如：这条消息有没有权威来源支持？" :disabled="loading" />
      <button type="submit" class="primary" :disabled="loading || !question.trim()">
        {{ loading ? '查证中…' : '提问' }}
      </button>
    </form>
    <div class="examples">
      <button v-for="example in examples" :key="example" type="button" class="chip"
              :disabled="loading" @click="ask(example)">{{ example }}</button>
    </div>

    <!-- 查证过程：默认展开，查的时候逐步追加；答完以后仍在，读者自己决定收不收。 -->
    <details v-if="loading || assistantSteps.length" class="steps" open>
      <summary>查证过程（{{ assistantSteps.length }} 步{{ loading ? '，进行中' : '' }}）</summary>
      <ol aria-live="polite">
        <li v-for="(step, index) in assistantSteps" :key="index">
          <span class="step-what">{{ step.label }}<span class="muted">：{{ step.query }}</span></span>
          <span v-if="step.by === 'system'" class="muted small">（打开页面时自动查）</span>
          <span v-if="step.reason" class="step-why">{{ step.reason }}</span>
          <span v-if="step.outcome" class="step-got muted small">→ {{ step.outcome }}</span>
        </li>
        <li v-if="loading" class="thinking muted" role="status">正在想下一步…</li>
      </ol>
    </details>
    <Callout v-if="error" tone="bad" role="alert">{{ error }}</Callout>

    <template v-if="result">
      <Callout v-if="!result.available" tone="warn" role="alert">{{ result.reason }}</Callout>
      <template v-else>
        <Callout v-if="result.incomplete" tone="warn">查证步数已用完，下面的回答只基于已经查到的资料。</Callout>
        <ol v-if="result.answer.length" class="answer">
          <li v-for="(sentence, index) in result.answer" :key="index">
            {{ sentence.text }}
            <span class="cites">
              <a v-for="handle in sentence.sources" :key="handle" :href="`#src-${handle}`"
                 class="cite" :aria-label="`出处 ${handle}`">{{ handle }}</a>
            </span>
          </li>
        </ol>
        <p v-else class="muted">平台资料里没有找到能回答这个问题的内容。</p>
        <p v-if="result.dropped" class="muted small">
          另有 {{ result.dropped }} 句因为指不出平台里的出处，已被删去。
        </p>

        <h3 v-if="Object.keys(result.sources).length" class="sub">出处</h3>
        <ul class="sources">
          <li v-for="(source, handle) in result.sources" :id="`src-${handle}`" :key="handle">
            <span class="handle">{{ handle }}</span>
            <span class="kind">{{ KIND_LABELS[source.kind] || source.kind }}</span>
            <RouterLink v-if="isInternal(source.link)" :to="source.link">{{ source.label || '查看' }}</RouterLink>
            <a v-else-if="source.link" :href="source.link" target="_blank" rel="noopener">
              {{ source.label || '原文' }}</a>
            <span v-else>{{ source.label }}</span>
            <span v-if="source.page" class="muted small">第 {{ source.page }} 页</span>
            <blockquote v-if="source.excerpt" class="excerpt">{{ source.excerpt }}</blockquote>
          </li>
        </ul>
      </template>

    </template>
  </section>
</template>

<style scoped>
.assistant { margin-bottom: 12px; }
.head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; }
.ask { display: flex; gap: 8px; margin-top: var(--sp-2); }
.ask input { flex: 1; min-width: 0; }
.ask .primary { background: var(--series-1); border-color: var(--series-1); color: var(--surface-1); }
.ask .primary:hover:not(:disabled) { background: var(--series-1); color: var(--surface-1); opacity: 0.9; }
.examples { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 4px; }
.chip { border-radius: 999px; padding: 3px 10px; min-height: 0; font-size: var(--fs-aux);
  color: var(--text-secondary); }
.answer { margin: 12px 0 6px; padding-left: 1.4em; line-height: 1.75; }
.answer li { margin-bottom: 4px; }
.cites { margin-left: 4px; }
.cite { display: inline-block; margin-right: 3px; padding: 0 5px; border-radius: 4px;
  border: 1px solid var(--gridline); font-size: var(--fs-note); line-height: 1.5;
  color: var(--series-1); text-decoration: none; vertical-align: 1px; }
.cite:hover { background: var(--hover-wash); }
.sub { font-size: var(--fs-body); margin: 14px 0 6px; }
.sources { list-style: none; margin: 0; padding: 0; font-size: var(--fs-aux); }
.sources li { padding: 6px 0; border-top: 1px solid var(--gridline); display: flex;
  flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
.sources li:target { background: var(--hover-wash); }
.handle { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--text-secondary); }
.kind { color: var(--text-muted); font-size: var(--fs-note); }
.excerpt { flex-basis: 100%; margin: 2px 0 0; padding-left: 10px; border-left: 2px solid var(--gridline);
  color: var(--text-secondary); font-size: var(--fs-note); line-height: 1.6; }
.steps { margin-top: 10px; font-size: var(--fs-aux); }
.steps li { margin-bottom: 4px; }
.step-why { display: block; color: var(--text-secondary); }
.thinking::marker { color: var(--text-muted); }
.thinking { animation: breathe 1.4s ease-in-out infinite; }
@keyframes breathe { 50% { opacity: 0.45; } }
@media (prefers-reduced-motion: reduce) { .thinking { animation: none; } }
.steps summary { cursor: pointer; color: var(--text-muted); }
.steps ol { margin: 6px 0 0; padding-left: 1.4em; }
</style>
