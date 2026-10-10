<script setup>
/**
 * 账号管理。
 *
 * 在这之前平台没有这一层：账号是手写的 `config/accounts.json`，忘了口令只能上
 * 服务器改文件重算哈希。四个动作放在这里——建、改角色、重设口令、停用——外加
 * 任何人都能改自己的口令。
 *
 * 停用而不是删除：审计日志、复核判定与处置记录里都留着这个人的名字。
 */
import { computed, onMounted, ref } from 'vue'

import { api, formatDate } from '../api.js'
import AsyncSection from '../components/ui/AsyncSection.vue'
import Callout from '../components/ui/Callout.vue'
import DataTable from '../components/ui/DataTable.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import Select from '../components/ui/Select.vue'
import { allows, session } from '../session.js'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const draft = ref({ username: '', password: '', role: 'viewer' })
const own = ref({ current: '', password: '' })
// 每个表单的错误就近显示，并由输入框的 aria-describedby 指过去（I-07）：页顶的
// Callout 离输入框太远，读屏用户提交后听不到是哪一格出了问题。
const ownError = ref('')
const createError = ref('')
const resetting = ref({ username: '', password: '', error: '' })
const MIN_PASSWORD = 8

const isAdmin = computed(() => allows('admin'))
const roleOptions = computed(() =>
  (data.value?.roles || []).map((role) => ({ value: role.value, label: role.label })))

const COLUMNS = [
  { key: 'username', title: '账号' },
  { key: 'role', title: '角色', slot: 'role', width: '150px' },
  { key: 'active', title: '状态', slot: 'active', nowrap: true, width: '96px' },
  { key: 'last_login_at', title: '最近登录', slot: 'last', nowrap: true },
  { key: 'created_by', title: '由谁建的', slot: 'created' },
]

async function load() {
  if (!isAdmin.value) { loading.value = false; return }
  loading.value = true
  error.value = ''
  try {
    data.value = await api.users()
  } catch (exception) {
    error.value = exception.message
  } finally {
    loading.value = false
  }
}

/** `fail` 决定错误写到哪里：缺省写页顶，表单动作写到表单自己的提示位。
 *  返回是否成功，调用方据此决定要不要清空表单。 */
async function run(action, message, fail = (text) => { error.value = text }) {
  error.value = ''
  notice.value = ''
  fail('')
  try {
    await action()
  } catch (exception) {
    fail(exception.message)
    return false
  }
  notice.value = message
  await load()
  return true
}

const create = () => run(
  () => api.userCreate({ ...draft.value }),
  `已建账号 ${draft.value.username}`,
  (text) => { createError.value = text },
).then((ok) => { if (ok) draft.value = { username: '', password: '', role: 'viewer' } })

const changeRole = (row, role) => run(
  () => api.userRole(row.username, role), `${row.username} 的角色已改`)

const toggleActive = (row) => run(
  () => api.userActive(row.username, !row.active),
  `${row.username} 已${row.active ? '停用' : '启用'}`)

// 重设密码原先走 window.prompt：输入的密码明文显示，对话框也没有可读的字段名。
// 现在在该行下方展开一个带标签的小表单。
function startReset(row) {
  resetting.value = { username: row.username, password: '', error: '' }
}
const cancelReset = () => { resetting.value = { username: '', password: '', error: '' } }

const submitReset = () => {
  const { username, password } = resetting.value
  return run(
    () => api.userResetPassword(username, password),
    `${username} 的密码已重设`,
    (text) => { resetting.value.error = text },
  ).then((ok) => { if (ok) cancelReset() })
}

const changeOwn = () => run(
  () => api.changeOwnPassword(own.value.current, own.value.password),
  '密码已改',
  (text) => { ownError.value = text },
).then((ok) => { if (ok) own.value = { current: '', password: '' } })

/** 提示与错误两段说明都挂到输入框上；错误不在时只挂提示。 */
const describedBy = (hint, errorId, text) => (text ? `${hint} ${errorId}` : hint)

onMounted(load)
</script>

<template>
  <PageHeader title="账号权限"
      note="平台账号与它们的角色；角色的意义只在复核环节。" />

  <Callout v-if="notice" tone="ok">{{ notice }}</Callout>
  <Callout v-if="error" tone="bad">{{ error }}</Callout>

  <section class="card">
    <h2>改我的密码</h2>
    <form class="row" @submit.prevent="changeOwn">
      <label class="field">
        <span class="field-label">当前密码</span>
        <input v-model="own.current" type="password" autocomplete="current-password" required
               :aria-invalid="ownError ? 'true' : undefined"
               :aria-describedby="ownError ? 'own-pw-error' : undefined" />
      </label>
      <!-- 提示放在 <label> 外面：放在里面会被并进字段名，读成「新密码 至少 8 位」。 -->
      <div class="field">
        <label class="field-label" for="own-pw-new">新密码</label>
        <input id="own-pw-new" v-model="own.password" type="password" autocomplete="new-password" required
               :minlength="MIN_PASSWORD" :aria-invalid="ownError ? 'true' : undefined"
               :aria-describedby="describedBy('own-pw-hint', 'own-pw-error', ownError)" />
        <span id="own-pw-hint" class="hint muted small">至少 {{ MIN_PASSWORD }} 位</span>
      </div>
      <button type="submit">保存</button>
      <span class="muted small">当前身份：{{ session.username }}</span>
    </form>
    <p v-if="ownError" id="own-pw-error" class="form-error" role="alert">{{ ownError }}</p>
  </section>

  <template v-if="isAdmin">
    <section class="card">
      <h2>新建账号</h2>
      <form class="row" @submit.prevent="create">
        <label class="field">
          <span class="field-label">账号</span>
          <input v-model="draft.username" type="text" autocomplete="off" spellcheck="false"
                 autocapitalize="off" required
                 :aria-invalid="createError ? 'true' : undefined"
                 :aria-describedby="createError ? 'create-error' : undefined" />
        </label>
        <div class="field">
          <label class="field-label" for="create-pw">初始密码</label>
          <input id="create-pw" v-model="draft.password" type="password" autocomplete="new-password" required
                 :minlength="MIN_PASSWORD" :aria-invalid="createError ? 'true' : undefined"
                 :aria-describedby="describedBy('create-pw-hint', 'create-error', createError)" />
          <span id="create-pw-hint" class="hint muted small">至少 {{ MIN_PASSWORD }} 位</span>
        </div>
        <div class="field">
          <span class="field-label" aria-hidden="true">角色</span>
          <Select v-model="draft.role" :options="roleOptions" label="角色" />
        </div>
        <button type="submit">建立</button>
      </form>
      <p v-if="createError" id="create-error" class="form-error" role="alert">{{ createError }}</p>
    </section>

    <section class="card">
      <AsyncSection :loading="loading" :error="error" :empty="!data?.items.length"
                    empty-text="还没有任何账号。">
        <DataTable :columns="COLUMNS" :rows="data?.items || []" row-key="username">
          <template #role="{ row }">
            <Select :model-value="row.role" :options="roleOptions" label="角色"
                    @update:model-value="(role) => changeRole(row, role)" />
          </template>
          <template #active="{ row }">
            <span :class="row.active ? '' : 'muted'">{{ row.active ? '启用' : '已停用' }}</span>
          </template>
          <template #last="{ row }">
            <span class="muted small">{{ row.last_login_at ? formatDate(row.last_login_at) : '从未' }}</span>
          </template>
          <template #created="{ row }">
            <span class="muted small">{{ row.created_by || '—' }}</span>
            <div class="actions">
              <button type="button" :aria-expanded="resetting.username === row.username"
                      @click="startReset(row)">重设密码</button>
              <button type="button" @click="toggleActive(row)">
                {{ row.active ? '停用' : '启用' }}
              </button>
            </div>
            <form v-if="resetting.username === row.username" class="row reset"
                  @submit.prevent="submitReset">
              <div class="field">
                <label class="field-label" for="reset-pw">{{ row.username }} 的新密码</label>
                <input id="reset-pw" v-model="resetting.password" type="password" autocomplete="new-password"
                       required :minlength="MIN_PASSWORD"
                       :aria-invalid="resetting.error ? 'true' : undefined"
                       :aria-describedby="describedBy('reset-pw-hint', 'reset-pw-error', resetting.error)" />
                <span id="reset-pw-hint" class="hint muted small">至少 {{ MIN_PASSWORD }} 位</span>
              </div>
              <button type="submit">确定</button>
              <button type="button" @click="cancelReset">取消</button>
              <p v-if="resetting.error" id="reset-pw-error" class="form-error" role="alert">
                {{ resetting.error }}
              </p>
            </form>
          </template>
        </DataTable>
      </AsyncSection>
    </section>
  </template>

  <!-- 不隐藏这一块：一个被藏起来的能力和一个不存在的能力，对使用者来说长得一样。 -->
  <Callout v-else tone="info">账号列表与增删需要管理员权限，当前角色可以改自己的密码。</Callout>
</template>

<style scoped>
h2 { font-size: var(--fs-title); font-weight: 600; margin: 0 0 var(--sp-3); }
.row { display: flex; gap: var(--sp-2); align-items: flex-start; flex-wrap: wrap; }
/* 持久可见的字段名（I-07）：placeholder 一输入就消失，读屏也不把它当名字。
   字段名行高固定，按钮与旁注下移同样的距离，于是和输入框顶边对齐；有没有
   「至少 8 位」提示都不影响对齐。 */
.field { --label-h: 20px; display: flex; flex-direction: column; gap: 4px;
         flex: 0 1 220px; min-width: 0; }
.field-label { height: var(--label-h); line-height: var(--label-h);
               font-size: var(--fs-aux); color: var(--text-secondary); }
.row > button, .row > .muted { margin-top: calc(20px + 4px); }
.row > .muted { line-height: 32px; }
.hint { line-height: 1.4; }
.form-error { color: var(--status-critical); margin: var(--sp-2) 0 0; font-size: var(--fs-aux); }
.reset { margin-top: var(--sp-2); }
.reset .form-error { flex-basis: 100%; }
.actions { display: flex; gap: var(--sp-2); margin-top: var(--sp-1); }
input { width: 100%; min-width: 0; box-sizing: border-box; }
</style>
