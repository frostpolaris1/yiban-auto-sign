import type { Page, Route } from '@playwright/test'
import type { Account, AccountVerifyResult } from '../../src/lib/api/account'

/**
 * 账号页的假 API。
 *
 * 一份路由处理全部 `/api/**` 请求。响应体只用登记过的合成值：
 * 手机号取 `tests/synthetic-fixtures.json` 里的号码再遮罩。
 * 调用记录供断言「请求带哪个参数」与「确认前发没发请求」。
 */

export type Role = 'master' | 'admin' | 'user'

export type ApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
  headers: Record<string, string>
}

/** 假会话的合成凭据。值已在仓库内登记过：邮箱是保留域，口令明示不是真口令。 */
export const SESSION_EMAIL = 'operator@example.com'
export const SESSION_PASSWORD = 'not-a-real-password'
export const SESSION_CSRF = 'csrf-test'

export const SCHOOLS = [
  {
    id: 'school-a',
    code: 'SYN-A',
    name: '合成学校甲',
    status: 'active',
    timezone: 'Asia/Shanghai',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'school-b',
    code: 'SYN-B',
    name: '合成学校乙',
    status: 'active',
    timezone: 'Asia/Shanghai',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
] as const

function account(overrides: Partial<Account> & Pick<Account, 'id'>): Account {
  return {
    school_id: 'school-a',
    owner_user_id: 'user-1',
    phone_masked: '138****8000',
    status: 'pending',
    reject_reason: '',
    reviewed_at: null,
    reviewed_by: null,
    created_at: '2026-10-01T01:00:00Z',
    updated_at: '2026-10-20T08:41:00Z',
    ...overrides,
  }
}

/** 三个状态各一条。掩码由登记号 13800138000 / 13900139000 / 13500010001 派生。 */
export const ACCOUNTS: Account[] = [
  account({ id: 'acc-1', phone_masked: '138****8000', status: 'pending' }),
  account({
    id: 'acc-2',
    school_id: 'school-b',
    owner_user_id: 'user-2',
    phone_masked: '139****9000',
    status: 'active',
  }),
  account({
    id: 'acc-3',
    phone_masked: '135****0001',
    status: 'paused_admin',
    reject_reason: '',
  }),
]

export type MockOptions = {
  role: Role | null
  /** 有会话时 me/login 返回的邮箱。 */
  email?: string
  /** login 端点接受的口令。 */
  password?: string
  /** 登录成功后采用的会话角色。初始 role 为 null 时用这个。 */
  loginRole?: Role
  mustChangePassword?: boolean
  /** 命中这些路径的请求返回 401，并清掉会话。用来模拟会话过期。 */
  expireOn?: string[]
  /** me 端点的人为延迟。用来观察守卫的加载态。 */
  meDelayMs?: number
  accounts?: Account[]
  /** 键是本次请求的 cursor；首屏键是空串。 */
  pages?: Record<string, Account[]>
  nextCursors?: Record<string, string>
  signError?: { status: number; code: string; message: string }
  verifyError?: { status: number; code: string; message: string }
  verifyResult?: AccountVerifyResult
}

export type FakeApi = {
  calls: ApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-accounts-api' }, trace_id: 'trace-test' },
  })
}

export async function mockAccountsApi(page: Page, options: MockOptions): Promise<FakeApi> {
  const calls: ApiCall[] = []
  // 会话角色随 login/logout/过期变化，故初始值拷贝一份。
  let sessionRole: Role | null = options.role
  // 每个测试一份副本：写操作会改状态，不能污染共享夹具。
  const allAccounts = (options.accounts ?? ACCOUNTS).map((item) => ({ ...item }))
  const pages = Object.fromEntries(
    Object.entries(options.pages ?? {}).map(([cursor, items]) => [
      cursor,
      items.map((item) => ({ ...item })),
    ]),
  )
  const knownAccounts = [...allAccounts, ...Object.values(pages).flat()]

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const rawBody = request.postData()
    let body: unknown = null
    if (rawBody) {
      try {
        body = JSON.parse(rawBody)
      } catch {
        body = rawBody
      }
    }
    calls.push({
      method: request.method(),
      path: url.pathname,
      search: url.search,
      params: Object.fromEntries(url.searchParams.entries()),
      body,
      headers: request.headers(),
    })

    // 会话过期：业务端点先失效一次，后续请求按未登录处理。
    if (
      sessionRole &&
      options.expireOn?.includes(url.pathname) &&
      !url.pathname.startsWith('/api/auth/')
    ) {
      sessionRole = null
      return fail(route, 401, 'unauthorized', '会话已过期')
    }

    const sessionUser = (role: Role) => ({
      id: 'user-1',
      email: options.email ?? SESSION_EMAIL,
      role,
      status: 'active' as const,
      must_change_password: Boolean(options.mustChangePassword),
      csrf_token: SESSION_CSRF,
    })

    if (url.pathname === '/api/auth/login' && request.method() === 'POST') {
      const credentials = (body ?? {}) as { email?: string; password?: string }
      if (
        credentials.email !== (options.email ?? SESSION_EMAIL) ||
        credentials.password !== (options.password ?? SESSION_PASSWORD)
      ) {
        return fail(route, 401, 'invalid_credentials', '邮箱或密码错误')
      }
      sessionRole = options.loginRole ?? 'master'
      return json(route, sessionUser(sessionRole))
    }

    if (url.pathname === '/api/auth/logout' && request.method() === 'POST') {
      sessionRole = null
      return route.fulfill({ status: 204 })
    }

    if (url.pathname === '/api/auth/me') {
      if (options.meDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.meDelayMs))
      }
      if (!sessionRole) return fail(route, 401, 'unauthorized', '会话失效')
      return json(route, sessionUser(sessionRole))
    }

    if (url.pathname === '/api/schools' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '无权访问')
      }
      return json(route, { items: SCHOOLS, next_cursor: '' })
    }

    if (url.pathname === '/api/accounts' && request.method() === 'GET') {
      const cursor = url.searchParams.get('cursor') ?? ''
      const items = pages[cursor] ?? allAccounts
      const next = options.nextCursors?.[cursor] ?? ''
      return json(route, { items, next_cursor: next })
    }

    const match = url.pathname.match(
      /^\/api\/accounts\/([^/]+)(?:\/(pause|sign|verify|review|location))?$/,
    )
    if (!match) return fail(route, 404, 'not_found', '未找到资源')
    const [, id, action] = match
    const found = knownAccounts.find((item) => item.id === id)
    if (!found) return fail(route, 404, 'not_found', '账号不存在')

    if (!action && request.method() === 'GET') return json(route, found)

    if (!action && request.method() === 'PATCH') {
      const updates = (body ?? {}) as Record<string, unknown>
      if (typeof updates.phone === 'string') {
        found.phone_masked = `${updates.phone.slice(0, 3)}****${updates.phone.slice(-4)}`
      }
      found.updated_at = '2026-10-28T09:30:00Z'
      return json(route, found)
    }
    if (!action && request.method() === 'DELETE') {
      const owned = allAccounts.findIndex((item) => item.id === found.id)
      if (owned >= 0) allAccounts.splice(owned, 1)
      for (const key of Object.keys(pages)) {
        const at = pages[key].findIndex((item) => item.id === found.id)
        if (at >= 0) pages[key].splice(at, 1)
      }
      return route.fulfill({ status: 204 })
    }
    if (action === 'location' && request.method() === 'PUT') {
      const update = (body ?? {}) as {
        mode: 'auto' | 'fixed'
        lng?: number
        lat?: number
        reason?: string
      }
      return json(route, {
        mode: update.mode,
        lng: update.mode === 'fixed' ? update.lng ?? null : null,
        lat: update.mode === 'fixed' ? update.lat ?? null : null,
        reason: update.reason ?? '',
      })
    }

    if (action === 'pause' && request.method() === 'POST') {
      const paused = Boolean((body as { paused?: boolean } | null)?.paused)
      found.status = paused ? 'paused_admin' : 'active'
      return json(route, found)
    }
    if (action === 'sign' && request.method() === 'POST') {
      if (options.signError) {
        return fail(route, options.signError.status, options.signError.code, options.signError.message)
      }
      return json(route, { task_id: 'task-1' }, 202)
    }
    if (action === 'verify' && request.method() === 'POST') {
      if (options.verifyError) {
        return fail(
          route,
          options.verifyError.status,
          options.verifyError.code,
          options.verifyError.message,
        )
      }
      return json(route, options.verifyResult ?? { ok: true, code: 'success' })
    }
    if (action === 'review' && request.method() === 'POST') {
      const decision = (body as { decision?: string } | null)?.decision
      return json(route, { ...found, status: decision === 'reject' ? 'rejected' : 'active' })
    }

    return fail(route, 405, 'internal_error', '不支持的方法')
  })

  return { calls }
}

export function listCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/accounts')
}

export function lastListCall(api: FakeApi): ApiCall | undefined {
  return listCalls(api).at(-1)
}

export function postCalls(api: FakeApi, id: string, action: string): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === `/api/accounts/${id}/${action}`,
  )
}

export function patchCalls(api: FakeApi, id: string): ApiCall[] {
  return api.calls.filter((call) => call.method === 'PATCH' && call.path === `/api/accounts/${id}`)
}

export function locationCalls(api: FakeApi, id: string): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'PUT' && call.path === `/api/accounts/${id}/location`,
  )
}

export function deleteCalls(api: FakeApi, id: string): ApiCall[] {
  return api.calls.filter((call) => call.method === 'DELETE' && call.path === `/api/accounts/${id}`)
}

export function loginCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter((call) => call.method === 'POST' && call.path === '/api/auth/login')
}

export function logoutCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter((call) => call.method === 'POST' && call.path === '/api/auth/logout')
}
