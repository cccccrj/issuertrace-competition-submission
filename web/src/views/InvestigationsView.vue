<script setup>
/**
 * 专项核查 — 谁在查什么、查到哪一步、什么时候到期、逾期几条。
 *
 * 不建第二套任务实体：一条核查就是一条处置记录，状态、责任人、系统建议、人工选择、
 * 说明、流水全都已经在那里，只多了一个截止时间。再建一张「核查任务」表，就是给同一件事
 * 第二套状态机——任务完成了而事项还在核查中，谁说了算？
 *
 * 与「标注复核」（/review）不是一回事：那是**模型标注**的复核队列，问的是「模型判得对
 * 不对」；这里是**业务事项**的核查，问的是「这条风险后来怎么办了」。两页都保留，名字分开。
 *
 * 排序由服务端给：逾期的在最前，然后是有期限的，最后是没定期限的。页面不自己再排一遍——
 * 两份排序迟早会各说各话。
 */
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { DISPOSITION_STATUS_META, api, companyLabel, formatDate } from '../api.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import DataTable from '../components/ui/DataTable.vue'
import LevelBadge from '../components/LevelBadge.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import StatTile from '../components/StatTile.vue'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const owner = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.riskInvestigations(owner.value ? { owner: owner.value } : {})
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}
watch(owner, load)
load()

const TILES = computed(() => {
  const summary = data.value?.summary
  if (!summary) return []
  const by = summary.by_status
  return [
    // 退回补证就是「上级要求再查一轮」，算在核查中里，旁边说出其中几条是被退回的。
    { label: '核查中', value: (by.investigating?.count ?? 0) + (by.returned?.count ?? 0),
      note: by.returned?.count ? `其中退回补证 ${by.returned.count} 条` : '已发起核查，等结论' },
    { label: '待上级复核', value: by.reporting?.count ?? 0, note: '已上报，等核查负责人复核' },
    { label: '持续跟踪', value: by.tracking?.count ?? 0, note: '结论已定，事项未了' },
    // 逾期旁边说出没定期限的有几条：两件事分开数，逾期数才不会被「没人定过期限」撑大。
    { label: '已逾期', value: summary.overdue, note: `另有 ${summary.no_due} 条未定截止日` },
  ]
})

const COLUMNS = [
  { key: 'risk_level', title: '风险等级', slot: 'level', nowrap: true, width: '104px' },
  { key: 'headline', title: '事项', slot: 'headline' },
  { key: 'status', title: '到哪一步', slot: 'status', nowrap: true, width: '96px' },
  { key: 'owner', title: '责任人', slot: 'owner', nowrap: true, width: '84px' },
  { key: 'due', title: '截止', slot: 'due', nowrap: true, width: '120px' },
  { key: 'latest', title: '最近进展', slot: 'latest', nowrap: true, width: '108px' },
]
</script>

<template>
  <PageHeader title="专项核查" note="跟踪风险事项的核查任务，包括负责人、处置进度与办理时限。" />

  <p class="muted small relation">
    本页用于风险事项的业务核查。系统研判结果的人工复核请前往
    <RouterLink to="/review">标注复核</RouterLink>，二者职责不同。
  </p>

  <AsyncSection :loading="loading" :error="error" :empty="false" empty-text="—">
    <div class="tiles">
      <StatTile v-for="tile in TILES" :key="tile.label"
                :label="tile.label" :value="tile.value" :note="tile.note" />
    </div>

    <div v-if="data.summary.by_owner.length" class="owners">
      <span class="muted small">按责任人：</span>
      <button type="button" :class="{ on: !owner }" @click="owner = ''">全部</button>
      <button v-for="row in data.summary.by_owner" :key="row.owner ?? '-'" type="button"
              :class="{ on: owner === row.owner }" :disabled="!row.owner"
              @click="owner = row.owner">
        {{ row.owner || '未指派' }} {{ row.count }}
        <span v-if="row.overdue" class="late">· 逾期 {{ row.overdue }}</span>
      </button>
    </div>

    <p v-if="!data.items.length" class="muted small empty">
      暂无进行中的专项核查。在风险研判工作台对事项执行「发起专项核查」后，将在此显示。
    </p>
    <div v-else class="card">
      <DataTable :columns="COLUMNS" :rows="data.items" row-key="alert_id">
        <template #level="{ row }">
          <LevelBadge :level="row.risk_level" :score="row.adjusted_risk_score" />
        </template>
        <template #headline="{ row }">
          <RouterLink :to="`/risks/${row.alert_id}`" class="headline">{{ row.headline }}</RouterLink>
          <div class="muted small">{{ companyLabel(row.company_id) }}</div>
        </template>
        <template #status="{ row }">
          <!-- 颜色、图标、文字同时在场：状态不靠颜色单独承载。 -->
          <span class="state" :style="{ color: DISPOSITION_STATUS_META[row.disposition.status]?.color }">
            <span aria-hidden="true">{{ DISPOSITION_STATUS_META[row.disposition.status]?.icon }}</span>
            {{ row.disposition.status_label }}
          </span>
        </template>
        <template #owner="{ row }">
          <span v-if="row.disposition.owner">{{ row.disposition.owner }}</span>
          <span v-else class="muted small">未指派</span>
        </template>
        <template #due="{ row }">
          <span v-if="row.disposition.due_at" class="mono" :class="{ late: row.disposition.overdue }">
            {{ row.disposition.due_at }}<template v-if="row.disposition.overdue"> · 逾期</template>
          </span>
          <span v-else class="muted small">未定</span>
        </template>
        <template #latest="{ row }">
          <span class="mono">{{ formatDate(row.latest_progress) }}</span>
        </template>
      </DataTable>
    </div>
  </AsyncSection>
</template>

<style scoped>
.relation { margin: 0 0 var(--sp-3); }
.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-3); margin-bottom: var(--sp-3); }
.owners { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-3); }
.owners button.on { border-color: var(--series-1); color: var(--series-1); }
.headline { font-weight: 600; }
.state { display: inline-flex; gap: 4px; align-items: center; white-space: nowrap; }
/* 逾期用文字「逾期」而不只用颜色：色觉差异的读者一样读得出来。 */
.late { color: var(--status-serious); font-weight: 600; }
.empty { margin: var(--sp-4) 0; }
@media (max-width: 720px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
</style>
