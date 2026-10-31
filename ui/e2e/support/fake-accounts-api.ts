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

/**
 * 合成重置令牌。
 *
 * 形状与真实令牌一致：32 字节随机数经 base64url 无填充编码，共 43 个字符。
 * 值取单字符重复，属登记表口径的合成值，不对应任何真实对象。
 */
export const RESET_TOKEN = 'A'.repeat(43)

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
    deleted_at: null,
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

/** 软删行夹具：只在 include_deleted=true 时出现。手机号取登记号 13100000000。 */
export const DELETED_ACCOUNTS: Account[] = [
  account({
    id: 'acc-del',
    phone_masked: '131****0000',
    status: 'pending',
    deleted_at: '2026-10-25T00:00:00Z',
  }),
]

/** 归属选择器的合成用户。邮箱是保留域 example.com。 */
export const LOOKUP_USERS = [
  { id: 'user-target', email: 'member.target@example.com', status: 'active' },
  { id: 'user-disabled', email: 'disabled.one@example.com', status: 'disabled' },
  // 同首字符同域对：掩码都是 a***@example.com（审查 F1 的绑定歧义）。
  { id: 'user-a-one', email: 'a.one@example.com', status: 'active' },
  { id: 'user-a-two', email: 'a.two@example.com', status: 'active' },
  // 无点域名：服务端 ValidEmail 允许（裁决 P3-5），UI 不得拦（F3）。
  { id: 'user-intranet', email: 'ops@intranet', status: 'active' },
]

/** 设备刷新的合成回读。掩码由登记形状的设备码派生（明文不出现）。 */
export const DEVICE_READ = {
  device_code_masked: 'DE****12',
  phone_model: 'Mi 9 合成机型',
  authorized: true,
} as const

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
  /** 忘记密码的失败注入。缺省恒 204，与契约一致。 */
  forgotError?: { status: number; code: string; message: string }
  /** 忘记密码断网：fetch 直接拒绝。 */
  forgotNetworkError?: boolean
  /** 重置密码的失败注入。缺省恒 204。 */
  resetError?: { status: number; code: string; message: string }
  /** 重置密码断网：fetch 直接拒绝。 */
  resetNetworkError?: boolean
  /** 登录态改密的失败注入。缺省 204，且成功后清掉 must_change_password。 */
  changePasswordError?: { status: number; code: string; message: string }
  /**
   * 改密时会话已失效：改密与随后的 me 探针都按未登录回 401。
   * 模拟中间件在业务处理前拒绝请求，而不是口令校验失败。
   */
  changePasswordSessionExpired?: boolean
  /** 登录态改密断网：fetch 直接拒绝。 */
  changePasswordNetworkError?: boolean
  /** 建号的失败注入。缺省 201 并把行并进列表。 */
  createError?: { status: number; code: string; message: string }
  /** 设备刷新的失败注入。缺省回 DEVICE_READ。 */
  deviceRefreshError?: { status: number; code: string; message: string }
  /** 恢复的失败注入。缺省清掉软删标记。 */
  restoreError?: { status: number; code: string; message: string }
  /** 归属选择器 lookup 的失败注入。 */
  lookupError?: { status: number; code: string; message: string }
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
  // 改密成功会清掉待改密标记，故这一位也要可变。
  let mustChangePassword = Boolean(options.mustChangePassword)
  // 每个测试一份副本：写操作会改状态，不能污染共享夹具。
  const allAccounts = (options.accounts ?? ACCOUNTS).map((item) => ({ ...item }))
  const pages = Object.fromEntries(
    Object.entries(options.pages ?? {}).map(([cursor, items]) => [
      cursor,
      items.map((item) => ({ ...item })),
    ]),
  )
  // 软删行每份测试一个副本：恢复会改行内标记，不能污染共享夹具。
  const deletedAccounts = DELETED_ACCOUNTS.map((item) => ({ ...item }))
  const knownAccounts = [...allAccounts, ...Object.values(pages).flat(), ...deletedAccounts]

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
      must_change_password: mustChangePassword,
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

    // 忘记密码：契约恒 204，不区分邮箱是否存在。
    // 回包不带任何账号线索，界面也无从泄露。
    if (url.pathname === '/api/auth/forgot-password' && request.method() === 'POST') {
      if (options.forgotNetworkError) return route.abort('failed')
      if (options.forgotError) {
        const { status, code, message } = options.forgotError
        return fail(route, status, code, message)
      }
      return route.fulfill({ status: 204 })
    }

    // 重置密码：成功 204；失败由用例注入（401 表示令牌无效、已用或过期）。
    if (url.pathname === '/api/auth/reset-password' && request.method() === 'POST') {
      if (options.resetNetworkError) return route.abort('failed')
      if (options.resetError) {
        const { status, code, message } = options.resetError
        return fail(route, status, code, message)
      }
      return route.fulfill({ status: 204 })
    }

    // 登录态改密：成功 204 且清掉待改密标记。
    // 401 有两种来源：旧口令错，或会话已在中间件失效。
    // 409 是新旧相同。界面靠随后的 me 探针区分前两种。
    if (url.pathname === '/api/auth/change-password' && request.method() === 'POST') {
      if (options.changePasswordSessionExpired) {
        sessionRole = null
        return fail(route, 401, 'unauthorized', 'authentication required')
      }
      if (options.changePasswordNetworkError) return route.abort('failed')
      if (options.changePasswordError) {
        const { status, code, message } = options.changePasswordError
        return fail(route, status, code, message)
      }
      mustChangePassword = false
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
      // UX-2 起契约对 user 放宽：三角色都可列学校（user 只见 active 行）。
      return json(route, { items: SCHOOLS, next_cursor: '' })
    }

    // 归属选择器的受限查询：master/admin 专用，只回 id、掩码邮箱、状态。
    if (url.pathname === '/api/users/lookup' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可检索用户')
      }
      if (options.lookupError) {
        const { status, code, message } = options.lookupError
        return fail(route, status, code, message)
      }
      const q = (url.searchParams.get('q') ?? '').toLowerCase()
      const exact = url.searchParams.get('exact') === 'true'
      // 真库按 email 升序（索引序）；假端点同口径，取第一条才有等价性。
      const hits = LOOKUP_USERS.filter((item) =>
        exact ? item.email === q : item.email.startsWith(q),
      ).sort((a, b) => a.email.localeCompare(b.email))
      const items = hits.map((item) => ({
        id: item.id,
        email_masked: `${item.email.slice(0, 1)}***${item.email.indexOf('@') > 0 ? item.email.slice(item.email.indexOf('@')) : ''}`,
        status: item.status,
      }))
      return json(route, { items })
    }

    // 建号：成功 201，行并进列表供后续断言。
    if (url.pathname === '/api/accounts' && request.method() === 'POST') {
      if (options.createError) {
        const { status, code, message } = options.createError
        return fail(route, status, code, message)
      }
      const input = (body ?? {}) as { school_id?: string; phone?: string; owner_user_id?: string }
      const created = account({
        id: 'acc-new',
        school_id: input.school_id ?? 'school-a',
        owner_user_id: input.owner_user_id ?? 'user-1',
        phone_masked: input.phone
          ? `${input.phone.slice(0, 3)}****${input.phone.slice(-4)}`
          : '138****8000',
      })
      allAccounts.push(created)
      knownAccounts.push(created)
      return json(route, created, 201)
    }

    if (url.pathname === '/api/accounts' && request.method() === 'GET') {
      const cursor = url.searchParams.get('cursor') ?? ''
      const includeDeleted = url.searchParams.get('include_deleted') === 'true'
      const base = pages[cursor] ?? allAccounts
      // 软删行只在 include_deleted=true 时入选，形状与服务端一致。
      const items = includeDeleted ? [...base, ...deletedAccounts] : base
      const next = options.nextCursors?.[cursor] ?? ''
      return json(route, { items, next_cursor: next })
    }

    const match = url.pathname.match(
      /^\/api\/accounts\/([^/]+)(?:\/(pause|sign|verify|review|location|device-refresh|restore))?$/,
    )
    if (!match) return fail(route, 404, 'not_found', '未找到资源')
    const [, id, action] = match
    const found = knownAccounts.find((item) => item.id === id)
    if (!found) return fail(route, 404, 'not_found', '账号不存在')

    if (!action && request.method() === 'GET') {
      // 详情与列表同口径：已删行不可发现，读取即 404。
      if (found.deleted_at) return fail(route, 404, 'not_found', '账号不存在')
      return json(route, found)
    }

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

    // 设备刷新：无请求体。回读值缺省即 DEVICE_READ；错误由用例注入。
    if (action === 'device-refresh' && request.method() === 'POST') {
      if (options.deviceRefreshError) {
        const { status, code, message } = options.deviceRefreshError
        return fail(route, status, code, message)
      }
      return json(route, DEVICE_READ)
    }

    // 恢复：只认软删行；未删行与服务端同判 409。
    if (action === 'restore' && request.method() === 'POST') {
      if (options.restoreError) {
        const { status, code, message } = options.restoreError
        return fail(route, status, code, message)
      }
      if (!found.deleted_at) {
        return fail(route, 409, 'conflict', '账号未删除')
      }
      found.deleted_at = null
      const live = allAccounts.find((item) => item.id === found.id)
      if (!live) allAccounts.push(found)
      return json(route, found)
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

export function forgotPasswordCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === '/api/auth/forgot-password',
  )
}

export function resetPasswordCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === '/api/auth/reset-password',
  )
}

export function createCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter((call) => call.method === 'POST' && call.path === '/api/accounts')
}

export function deviceRefreshCalls(api: FakeApi, id: string): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === `/api/accounts/${id}/device-refresh`,
  )
}

export function restoreCalls(api: FakeApi, id: string): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === `/api/accounts/${id}/restore`,
  )
}

export function lookupCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/users/lookup')
}

export function changePasswordCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === '/api/auth/change-password',
  )
}
