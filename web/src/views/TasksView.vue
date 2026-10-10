<script setup>
/**
 * 我的任务 — 轮到我做的事，每条说清楚是什么、谁交来的、要我做什么。
 *
 * 此前这一格叫「我的待办」，其实是风险事项库加一个「负责人 = 我」的筛选：每行只有
 * 一个「打开研判档案」，读不出这一条是别人上报来等我复核的，还是被退回来要我补证的
 * （修改思路 v2 §七）。
 *
 * **不建第二套任务实体。** 任务类型由处置状态推出，谁交来的由处置流水读出——服务端
 * `GET /api/risks/tasks` 做这两件事，这一页只负责画。
 */
import { computed, ref } from 'vue'

import { api, companyLabel, formatDate } from '../api.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import LevelBadge from '../components/LevelBadge.vue'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const kind = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.tasks()
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}
load()

/* 类型切换只列有任务的那几类，再加一个「全部」：五个按钮里三个是 0，读者会以为自己漏了什么。 */
const CHIPS = computed(() => [
  { key: '', label: '全部', count: data.value?.items.length ?? 0 },
  ...Object.entries(data.value?.kinds || {})
    .filter(([key]) => data.value.counts[key])
    .map(([key, meta]) => ({ key, label: meta.label, count: data.value.counts[key] })),
])
const shown = computed(() => (data.value?.items || [])
  .filter((item) => !kind.value || item.task.kind === kind.value))
</script>

<template>
  <PageHeader title="我的任务" note="分配给您的待办任务，包括来源、原因与所需操作。">
    <template #basis>
      <p>
        每条任务对应一条由您负责且尚未归档的风险事项，任务类型取决于事项所处的处置阶段。
        被退回的任务优先排列，需尽快补充证据。
      </p>
    </template>
  </PageHeader>

  <AsyncSection :loading="loading" :error="error" :empty="!data"
                empty-text="取不到任务列表。刷新一次；仍然如此，请查看服务是否在运行。">
    <nav v-if="data.items.length" class="chips" aria-label="任务类型">
      <button v-for="chip in CHIPS" :key="chip.key" type="button" class="chip"
              :class="{ on: kind === chip.key }" :aria-pressed="kind === chip.key"
              @click="kind = chip.key">
        {{ chip.label }} <span class="mono">{{ chip.count }}</span>
      </button>
    </nav>

    <article v-for="item in shown" :key="item.alert_id" class="card task" :class="item.task.kind">
      <div class="task-main">
        <div class="task-head">
          <span class="kind">{{ item.task.label }}</span>
          <LevelBadge :level="item.risk_level" :score="item.adjusted_risk_score" />
          <span class="muted small">{{ companyLabel(item.company_id) }}</span>
          <span v-if="item.disposition.due_at" class="small" :class="item.disposition.overdue ? 'late' : 'muted'">
            截止 {{ item.disposition.due_at }}<template v-if="item.disposition.overdue"> · 已逾期</template>
          </span>
        </div>
        <b class="headline">{{ item.headline }}</b>
        <p class="small from">
          <template v-if="item.handed_by">
            <b>{{ item.handed_by.actor }}</b> 于 {{ formatDate(item.handed_by.occurred_at) }} 交来
            <template v-if="item.handed_by.note">：「{{ item.handed_by.note }}」</template>
          </template>
          <template v-else>自己认领</template>
        </p>
        <p class="muted small">
          待办事项：{{ item.task.detail }}
          <template v-if="item.disposition.conclusion_label"> · 上一步结论：{{ item.disposition.conclusion_label }}</template>
        </p>
      </div>
      <RouterLink class="go" :to="`/risks/${item.alert_id}#decide`">{{ item.task.action }} →</RouterLink>
    </article>

    <p v-if="!data.items.length" class="empty">
      暂无分配给您的任务。
      <RouterLink v-if="data.unclaimed" to="/risks?status=pending">
        另有 {{ data.unclaimed }} 条待研判事项尚未认领 →
      </RouterLink>
    </p>
    <p v-else-if="data.unclaimed" class="muted small more">
      另有 <RouterLink to="/risks?status=pending">{{ data.unclaimed }} 条待研判事项</RouterLink>
      尚未认领。
    </p>
  </AsyncSection>
</template>

<style scoped>
.chips { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-3); }
.chip.on { border-color: var(--series-1); color: var(--text-primary); font-weight: 600; }
.task { display: flex; gap: var(--sp-4); align-items: center; margin-bottom: var(--sp-2);
  border-left: 2px solid var(--border); }
/* 左缘颜色按紧急度：被退回的最急。颜色之外还有类型文字，不靠颜色单独承载。 */
.task.returned { border-left-color: var(--status-critical); }
.task.reporting { border-left-color: var(--status-serious); }
.task.investigating { border-left-color: var(--series-1); }
.task-main { flex: 1 1 auto; min-width: 0; }
.task-head { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap;
  margin-bottom: var(--sp-1); }
.kind { font-weight: 600; font-size: var(--fs-aux); }
.headline { display: block; margin-bottom: 2px; }
.from { margin: 2px 0; color: var(--text-secondary); }
.go { flex: none; font-weight: 600; white-space: nowrap; }
.late { color: var(--status-serious); font-weight: 600; }
.more { margin-top: var(--sp-3); }
@media (max-width: 640px) { .task { flex-direction: column; align-items: flex-start; } }
</style>
