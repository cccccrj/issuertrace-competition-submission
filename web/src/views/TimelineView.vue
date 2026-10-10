<script setup>
/**
 * 时间线与披露阶段 — the brief's fifth task.
 *
 * Migrated onto the shared layer. Two things fixed while it was open: the
 * company filter defaulted to cxmt with no way to see that half the corpus was
 * hidden — the third page in this refactor with that exact defect — and the
 * three requests it makes ran one after another for no reason.
 *
 * The phase strip stays hand-built. Its bars are scaled by event count rather
 * than by calendar length on purpose (a one-week 上会 window matters as much as
 * a six-month one), and each carries the rationale for its coefficient in
 * words. A generic chart would keep the bar and lose the sentence.
 */
import { computed, ref, watch } from 'vue'

import { api, basisLabel, companyLabel, formatDate, issueLabel, tierLabel } from '../api.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import LevelBadge from '../components/LevelBadge.vue'
import Pager from '../components/Pager.vue'
import { debouncedField } from '../debounce.js'

const filters = ref({ company: '', phase: '', level: '', basis: '', search: '' })
const searchDraft = debouncedField(filters)
const newFactsOnly = ref(false)
const offset = ref(0)
const limit = 30
const data = ref(null)
const phases = ref([])
const round = ref(null)
const loading = ref(true)
const error = ref('')

const LEVEL_OPTIONS = { high: '高', medium: '中', low: '低' }
const BASIS_OPTIONS = { event_time: '事件时间', published_at: '发布时间' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = { ...filters.value, limit, offset: offset.value }
    if (newFactsOnly.value) params.new_facts_only = true
    const [page, phaseList, rounds] = await Promise.all([
      api.timeline(params),
      api.phases({ company: filters.value.company }),
      api.rounds(),
    ])
    data.value = page
    phases.value = phaseList.items
    round.value = rounds.items[0] || null
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

watch([filters, newFactsOnly], () => { offset.value = 0; load() }, { deep: true })
watch(offset, load)
load()

const active = computed(() =>
  Object.values(filters.value).filter(Boolean).length + (newFactsOnly.value ? 1 : 0))
function reset() {
  filters.value = { company: '', phase: '', level: '', basis: '', search: '' }
  newFactsOnly.value = false
}

const phaseOptions = computed(() => phases.value.map((phase) => ({
  value: phase.phase, label: phase.phase_label, count: phase.events,
})))

// The strip is drawn on event counts, not on calendar length: a one-week 上会
// window matters as much as a six-month one, and scaling by days would hide it.
const maxEvents = computed(() => Math.max(1, ...phases.value.map((phase) => phase.events)))

const COLUMNS = [
  { key: 'timeline_at', title: '时间', slot: 'time', nowrap: true, width: '110px' },
  { key: 'summary', title: '事件', slot: 'event' },
  { key: 'phase_label', title: '阶段', slot: 'phase', width: '128px' },
  { key: 'adjusted_risk_score', title: '风险分', slot: 'risk', width: '150px' },
  { key: 'new_fact_count', title: '新增事实', slot: 'facts', width: '92px' },
]
</script>

<template>
  <PageHeader title="事件脉络"
      note="事件按时间排开，叠上公司自己的申报阶段与阶段系数。">
  </PageHeader>

  <FilterBar :active="active" @reset="reset">
    <CompanySelect v-model="filters.company" />
    <EnumSelect v-model="filters.phase" :options="phaseOptions" all-label="全部阶段" />
    <EnumSelect v-model="filters.level" :labels="LEVEL_OPTIONS" all-label="全部风险等级" />
    <EnumSelect v-model="filters.basis" :labels="BASIS_OPTIONS" all-label="全部时间口径" />
    <label class="check">
      <input v-model="newFactsOnly" type="checkbox" /> 只看含新增事实的事件
    </label>
    <input v-model="searchDraft" type="search" placeholder="搜索摘要或标题…" aria-label="搜索摘要或标题" />
    <template #end>
      <span v-if="data" class="muted small">共 {{ data.total.toLocaleString() }} 条</span>
    </template>
  </FilterBar>

  <div class="card phase-card">
    <div class="phase-head">
      <b>披露阶段与风险系数</b>
      <span v-if="round" class="muted small">
        当前处理轮次 #{{ round.ordinal }} · 参照时间 {{ formatDate(round.reference_time) }}
        <template v-if="round.is_baseline">（基线轮，不判定新增）</template>
      </span>
    </div>
    <div v-if="!phases.length" class="muted small">该筛选下没有阶段数据。</div>
    <div v-else class="phases">
      <div v-for="phase in phases" :key="phase.company_id + phase.phase" class="phase">
        <div class="phase-top">
          <span class="phase-name">{{ phase.phase_label }}</span>
          <span class="mono coefficient" :class="{ raised: phase.coefficient > 1 }">
            ×{{ phase.coefficient }}
          </span>
        </div>
        <div class="muted phase-window mono">
          {{ formatDate(phase.started_at) || '—' }} → {{ phase.ended_at ? formatDate(phase.ended_at) : '至今' }}
        </div>
        <div class="bar" :style="{ width: `${(phase.events / maxEvents) * 100}%` }" aria-hidden="true"></div>
        <div class="muted small">
          {{ phase.events.toLocaleString() }} 个事件 ·
          均分 {{ phase.mean_base ?? '—' }} → {{ phase.mean_adjusted ?? '—' }}
        </div>
        <div class="rationale">{{ phase.rationale }}</div>
      </div>
    </div>
  </div>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    empty-text="没有符合条件的记录。"
  >
    <div class="card">
      <DataTable :columns="COLUMNS" :rows="data.items" row-key="event_id">
        <template #time="{ row }">
          <div class="mono">{{ formatDate(row.timeline_at) }}</div>
          <div class="muted basis">{{ basisLabel(row.time_basis) }}</div>
        </template>
        <template #event="{ row }">
          <div>{{ row.summary }}</div>
          <div class="muted small">
            {{ companyLabel(row.company_id) }} · {{ row.source_name || '—' }}（{{ tierLabel(row.source_tier) }}）
            <template v-if="row.issue_codes.length">
              · {{ row.issue_codes.map(issueLabel).join('、') }}
            </template>
          </div>
        </template>
        <template #phase="{ row }">
          <div>{{ row.phase_label }}</div>
          <div class="muted mono small">×{{ row.phase_coefficient }}</div>
        </template>
        <template #risk="{ row }">
          <LevelBadge :level="row.adjusted_risk_level" :score="row.adjusted_risk_score" />
          <!-- Showing the base score next to the adjusted one keeps the phase
               coefficient auditable instead of silently baked in. -->
          <div class="muted mono small">
            基础 {{ row.base_risk_score.toFixed(0) }} × {{ row.phase_coefficient }}
          </div>
        </template>
        <template #facts="{ row }">
          <template v-if="row.claim_count">
            <span class="mono">{{ row.new_fact_count }}</span>
            <span class="muted small"> / {{ row.claim_count }} 条主张</span>
          </template>
          <span v-else class="muted small">无主张</span>
        </template>
      </DataTable>
      <Pager :total="data.total" :limit="limit" :offset="offset" @update:offset="offset = $event" />
    </div>
  </AsyncSection>
</template>

<style scoped>
.basis { font-size: var(--fs-note); }
.check { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-aux); color: var(--text-secondary); }
.phase-card { margin-bottom: 14px; }
.phase-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }
.phases { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(178px, 100%), 1fr)); gap: 12px; }
.phase { border-left: 2px solid var(--gridline); padding-left: 10px; }
.phase-top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.phase-name { font-weight: 580; font-size: var(--fs-body); }
.coefficient { color: var(--text-muted); font-size: var(--fs-aux); }
.coefficient.raised { color: var(--status-serious); }
.phase-window { font-size: var(--fs-note); }
.bar { height: 6px; border-radius: 3px; background: var(--series-1); margin: 6px 0 4px; min-width: 3px; }
.rationale { font-size: var(--fs-note); color: var(--text-muted); margin-top: 4px; line-height: 1.5; }
</style>
