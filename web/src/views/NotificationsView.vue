<script setup>
/**
 * 通知箱 — the record that somebody was told, not a second set of conclusions.
 *
 * Migrated onto the shared layer. It keeps its card list rather than becoming a
 * table: a notification is read as a message, and a row of columns is not how
 * anyone reads a message.
 *
 * One thing added while it was open: a breakdown by kind and level. The page
 * showed 「未读 N / 共 M」 and nothing else, so a reader with sixty unread items
 * could not tell whether they were high-risk alerts or review chores without
 * filtering and counting by hand.
 */
import { computed, ref, watch } from 'vue'

import { COMPANY_LABELS, api, formatDate } from '../api.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import Pager from '../components/Pager.vue'

const data = ref(null)
const unreadOnly = ref(false)
const offset = ref(0)
const limit = 25
const loading = ref(true)
const error = ref('')

const KIND_LABELS = { alert: '风险预警', review_task: '复核任务', handoff: '交接给我' }
const LEVEL_LABELS = { high: '高', medium: '中', task: '待办' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.notifications({ unread: unreadOnly.value, limit, offset: offset.value })
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

async function readAll() {
  await api.markNotificationsRead()
  await load()
}

async function open(item) {
  // Reading is recorded before navigating: the audit answers "was this
  // delivered and seen", which a click that only navigates cannot.
  if (!item.read_at) await api.markNotificationsRead([item.notification_id])
  await load()
}

watch(offset, load)
load()

/** What is waiting, by kind — sixty unread items are a different afternoon
 *  depending on whether they are alerts or review chores. */
const mix = computed(() => {
  const counts = {}
  for (const item of data.value?.items || []) {
    const key = item.kind
    counts[key] = (counts[key] || 0) + 1
  }
  return Object.entries(counts).map(([key, count]) => ({
    label: KIND_LABELS[key] || key, count,
  }))
})
</script>

<template>
  <PageHeader title="通知箱"
      note="站内收到的预警通知，以及它们按类型与等级的构成。">
    <template #actions>
      <RouterLink to="/outbox" class="small">推送记录 →</RouterLink>
    </template>
  </PageHeader>

  <FilterBar :active="unreadOnly ? 1 : 0" @reset="unreadOnly = false; offset = 0; load()">
    <button type="button" @click="unreadOnly = !unreadOnly; offset = 0; load()">
      {{ unreadOnly ? '显示全部' : '只看未读' }}
    </button>
    <button type="button" :disabled="!data?.unread" @click="readAll">全部标记已读</button>
    <template #end>
      <span v-if="data" class="muted small">
        未读 <b class="mono">{{ data.unread }}</b> / 共 {{ data.total }}
        <template v-if="mix.length">
          · 本页
          <span v-for="(row, index) in mix" :key="row.label">
            {{ row.label }} {{ row.count }}<span v-if="index < mix.length - 1">、</span>
          </span>
        </template>
      </span>
    </template>
  </FilterBar>

  <AsyncSection
    :loading="loading"
    :error="error"
    :empty="!data?.items.length"
    :empty-text="unreadOnly ? '没有未读通知。' : '没有通知。预警与复核任务产生时会自动进箱。'"
  >
    <article v-for="item in data.items" :key="item.notification_id" class="card note"
             :class="{ unread: !item.read_at }">
      <header>
        <span class="tag">{{ KIND_LABELS[item.kind] || item.kind }}</span>
        <span class="tag" :class="item.level">{{ LEVEL_LABELS[item.level] || item.level }}</span>
        <span class="muted small">{{ COMPANY_LABELS[item.company_id] || item.company_id || '—' }}</span>
        <span class="muted small">{{ formatDate(item.created_at) }}</span>
        <span v-if="item.read_at" class="muted small">已读 {{ formatDate(item.read_at) }}</span>
      </header>
      <b>{{ item.title }}</b>
      <p class="muted small">{{ item.body }}</p>
      <RouterLink :to="item.link" class="small" @click="open(item)">打开 →</RouterLink>
    </article>
    <Pager :total="data.total" :limit="limit" :offset="offset" @update:offset="offset = $event" />
  </AsyncSection>
</template>

<style scoped>
.note { margin-bottom: 10px; }
.note.unread { border-left: 2px solid var(--status-serious); }
.note header { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; margin-bottom: 6px; }
.tag.high { color: var(--status-critical); }
.tag.medium { color: var(--status-warning); }
</style>
