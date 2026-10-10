<script setup>
/**
 * 我的预警订阅 — who gets told what.
 *
 * 修改思路 v2 §九：业务用户的第一屏只有「关注什么、怎么提醒」。推送地址、签名密钥、
 * 允许名单收进「技术配置」，只有管理员能填，其他人看得见有这回事、改不了。
 *
 * The demonstration switch is the most important control on the page, so it is
 * spelled out rather than shown as an icon: turning it off is what lets this
 * feature reach the network, and a reader should be able to tell at a glance
 * which of their rules can. The panel beside it names the allowlist, because an
 * operator who has to read the source to find out where a webhook may point
 * will guess instead.
 */
import { computed, onMounted, ref } from 'vue'

import {
  CHANNEL_LABELS, ISSUE_LABELS, RISK_LEVEL_LABELS, api, companyLabel, issueLabel,
} from '../api.js'
import { allows } from '../session.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import CompanySelect from '../components/ui/CompanySelect.vue'
import DataTable from '../components/ui/DataTable.vue'
import EnumSelect from '../components/ui/EnumSelect.vue'
import PageHeader from '../components/ui/PageHeader.vue'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const creating = ref(false)
/* 业务用户的默认是「站内」：不需要地址，也不会离开这台机器。Webhook 收在技术配置里。 */
const BLANK = {
  name: '', channel: 'inapp', target: '', company_id: '', issue_code: '',
  min_risk_level: 'high', reason_codes: [], secret_ref: '', quiet_hours: '',
}
const draft = ref({ ...BLANK, reason_codes: [] })
const admin = computed(() => allows('admin'))

/* 渠道说实话：邮件只渲染成 .eml 文件，平台不接 SMTP。v2 要的「企业微信」没有真实通道，
   不列——一个选了也不会发出去的选项，比没有这个选项更糟。 */
const CHANNEL_OPTIONS = computed(() => ({
  inapp: '站内通知',
  email: '邮件（演示：生成 .eml，不发送）',
  ...(admin.value ? { webhook: 'Webhook（技术配置）' } : {}),
}))

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.notifySubscriptions()
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

onMounted(load)

function toggleReason(code) {
  const codes = draft.value.reason_codes
  draft.value.reason_codes = codes.includes(code) ? codes.filter((c) => c !== code) : [...codes, code]
}

async function create() {
  creating.value = true
  error.value = ''
  notice.value = ''
  try {
    const body = { ...draft.value }
    for (const key of ['company_id', 'issue_code', 'secret_ref', 'quiet_hours', 'target']) {
      if (!body[key]) delete body[key]
    }
    await api.notifyCreate(body)
    notice.value = '订阅已创建，处于演示模式：它会算出该提醒什么，但不会真的发出。'
    draft.value = { ...BLANK, reason_codes: [] }
    await load()
  } catch (exception) {
    error.value = exception.message
  } finally {
    creating.value = false
  }
}

async function toggle(row, field) {
  error.value = ''
  notice.value = ''
  try {
    await api.notifyToggle(row.subscription_id, { [field]: !row[field] })
    await load()
  } catch (exception) {
    error.value = exception.message
  }
}

const live = computed(() => (data.value?.items || []).filter((row) => !row.dry_run && row.enabled))
const reasonLabel = (code) => data.value?.reasons?.[code] || code

const COLUMNS = [
  { key: 'name', title: '订阅', slot: 'name' },
  { key: 'scope', title: '关注什么', slot: 'scope' },
  { key: 'channel', title: '怎么提醒', slot: 'channel', nowrap: true, width: '160px' },
  { key: 'mode', title: '模式', slot: 'mode', nowrap: true, width: '150px' },
  { key: 'enabled', title: '启用', slot: 'enabled', nowrap: true, width: '84px' },
]
</script>

<template>
  <PageHeader title="我的预警订阅"
      note="设置关注的公司、专题与情形，以及命中后的提醒方式。">
  </PageHeader>

  <Callout v-if="notice" tone="ok">{{ notice }}</Callout>
  <Callout v-if="error" tone="bad">{{ error }}</Callout>

  <Callout v-if="live.length" tone="warn" title="注意">
    有 <b class="mono">{{ live.length }}</b> 条订阅已关闭演示模式，命中时会真实发出。
  </Callout>

  <section class="card">
    <h2>新建订阅</h2>
    <div class="form">
      <label>名称
        <input v-model="draft.name" type="search" placeholder="如：出口管制高风险提醒" />
      </label>
      <label>关注公司
        <CompanySelect v-model="draft.company_id" />
      </label>
      <label>关注专题
        <EnumSelect v-model="draft.issue_code" :labels="ISSUE_LABELS" all-label="全部专题" />
      </label>
      <label>最低风险等级
        <EnumSelect v-model="draft.min_risk_level" :labels="RISK_LEVEL_LABELS" all-label="—" />
      </label>
      <label>提醒方式
        <EnumSelect v-model="draft.channel" :labels="CHANNEL_OPTIONS" all-label="—" />
      </label>
      <label v-if="draft.channel === 'email'">邮箱
        <input v-model="draft.target" type="search" placeholder="name@example.com" />
      </label>
      <label>免打扰时段
        <input v-model="draft.quiet_hours" type="search" placeholder="22:00-07:00" />
      </label>
    </div>

    <fieldset v-if="data?.reasons" class="reasons">
      <legend>仅在出现以下情形时提醒（不选则达到风险等级即提醒）</legend>
      <label v-for="(label, code) in data.reasons" :key="code" class="check">
        <input type="checkbox" :checked="draft.reason_codes.includes(code)" @change="toggleReason(code)" />
        {{ label }}
      </label>
    </fieldset>

    <!-- 技术配置：推送地址、签名密钥、允许名单。业务用户用不到，也不该在第一屏看见它们；
         只有管理员能填，其他人看得见这里有什么、改不了。 -->
    <!-- 只对管理员显示（I-06）：变量名、允许名单是运维配置，业务角色看到的只有一句说明。 -->
    <p v-if="!admin" class="muted small">外部推送的地址与签名由管理员配置。</p>
    <details v-else class="tech">
      <summary>技术配置（推送渠道）</summary>
      <div class="form">
        <label>推送地址
          <input v-model="draft.target" type="search" :disabled="!admin || draft.channel !== 'webhook'"
                 placeholder="接收方的 Webhook 地址（https 开头）" />
        </label>
        <label>签名密钥变量名
          <input v-model="draft.secret_ref" type="search" :disabled="!admin || draft.channel !== 'webhook'"
                 placeholder="签名密钥所在的配置项" />
        </label>
      </div>
      <p class="muted small">
        此处填写<b>服务器配置项名称</b>而非密钥本身；密钥值仅在签名时读取，不入库，也不会出现在任何接口返回中。
        推送目标必须落在允许名单内：
        <code v-for="pattern in data?.allowlist || []" :key="pattern" class="mono">{{ pattern }}</code>
      </p>
    </details>

    <button type="button" class="create" :disabled="!draft.name || creating" @click="create">
      {{ creating ? '创建中…' : '创建订阅' }}
    </button>
  </section>

  <AsyncSection
    :loading="loading"
    :error="''"
    :empty="!data?.items.length"
    empty-text="暂无订阅。可在上方新建订阅，再到「上报与推送记录」执行评估，查看其提醒内容。"
  >
    <div class="card">
      <DataTable :columns="COLUMNS" :rows="data.items" row-key="subscription_id">
        <template #name="{ row }">
          <div>{{ row.name }}</div>
          <div class="muted small">由 {{ row.created_by }} 创建</div>
        </template>
        <template #scope="{ row }">
          {{ row.company_id ? companyLabel(row.company_id) : '全部公司' }}
          · {{ row.issue_code ? issueLabel(row.issue_code) : '全部专题' }}
          · 风险 ≥ {{ RISK_LEVEL_LABELS[row.min_risk_level] || row.min_risk_level }}
          <div v-if="row.reason_codes.length" class="muted small">
            仅当：{{ row.reason_codes.map(reasonLabel).join('；') }}
          </div>
          <div v-if="row.quiet_hours" class="muted small">免打扰 {{ row.quiet_hours }}</div>
        </template>
        <template #channel="{ row }">
          <div>{{ CHANNEL_LABELS[row.channel] || row.channel }}</div>
          <!-- 推送地址与密钥名只给管理员看：它们是技术配置，不是业务信息。 -->
          <template v-if="admin">
            <div class="muted small mono">{{ row.target || '—' }}</div>
            <div v-if="row.secret_ref" class="muted small">签名密钥：{{ row.secret_ref }}</div>
          </template>
        </template>
        <template #mode="{ row }">
          <button type="button" class="mode" :class="{ live: !row.dry_run }" @click="toggle(row, 'dry_run')">
            {{ row.dry_run ? '演示模式' : '真实发送' }}
          </button>
        </template>
        <template #enabled="{ row }">
          <button type="button" @click="toggle(row, 'enabled')">
            {{ row.enabled ? '已启用' : '已停用' }}
          </button>
        </template>
      </DataTable>
    </div>
  </AsyncSection>
</template>

<style scoped>
h2 { font-size: var(--fs-body); font-weight: 650; margin: 0 0 10px; }
.form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
label { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-aux); color: var(--text-secondary); }
input[type='search'] { min-width: 170px; }
.muted code { margin-right: 6px; }
.mode.live { border-color: var(--status-warning); color: var(--text-primary); }
.reasons { margin: var(--sp-3) 0 0; padding: 0; border: none; display: flex; flex-wrap: wrap; gap: 6px 16px; }
.reasons legend { font-size: var(--fs-aux); color: var(--text-secondary); margin-bottom: 6px; padding: 0; }
.check { flex-direction: row; align-items: center; gap: 6px; color: var(--text-primary); }
.tech { margin-top: var(--sp-3); font-size: var(--fs-aux); }
.tech summary { cursor: pointer; color: var(--text-secondary); margin-bottom: var(--sp-2); }
.create { margin-top: var(--sp-3); }
</style>
