import { reactive } from 'vue'

/** The signed-in state, read from the server rather than kept in localStorage:
 *  the cookie is HttpOnly, so the browser cannot inspect it and must ask. */
export const session = reactive({
  ready: false,
  authenticated: false,
  username: null,
  role: null,
})

/** Roles in ascending authority, mirroring api/auth.py. Hiding a control the
 *  role cannot use is a courtesy; the server still refuses the request. */
const ROLES = ['viewer', 'analyst', 'reviewer', 'admin']
export const ROLE_LABELS = { viewer: '只读', analyst: '分析师', reviewer: '核查负责人', admin: '管理员' }
export const allows = (required) =>
  ROLES.indexOf(session.role) >= 0 && ROLES.indexOf(session.role) >= ROLES.indexOf(required)

function apply(state) {
  session.authenticated = Boolean(state.authenticated)
  session.username = state.username || null
  session.role = state.role || null
  session.ready = true
}

export async function refreshSession() {
  try {
    apply(await (await fetch('/api/auth/session')).json())
  } catch {
    apply({ authenticated: false })
  }
  return session
}

export async function signIn(username, password) {
  let response
  try {
    response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
  } catch {
    throw new Error('网络连接异常，请检查网络后重试')
  }
  const body = await response.json().catch(() => ({}))
  // 服务端写的中文原样给出；其余（英文、结构化校验错误）换成一句通用的话（I-06）。
  if (!response.ok) {
    throw new Error(typeof body.detail === 'string' && /[\u4e00-\u9fff]/.test(body.detail)
      ? body.detail : '登录失败，请稍后重试')
  }
  apply(body)
  return session
}

export async function signOut() {
  await fetch('/api/auth/logout', { method: 'POST' }).catch(() => {})
  apply({ authenticated: false })
}

/** Called when any data request comes back unauthorised, so an expired session
 *  surfaces as a sign-in prompt rather than as a page full of failed panels. */
export function markSignedOut() {
  apply({ authenticated: false })
}
