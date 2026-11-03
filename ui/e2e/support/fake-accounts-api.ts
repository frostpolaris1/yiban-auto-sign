import { createHash } from 'node:crypto'
import type { Page, Request, Route } from '@playwright/test'
import type { Account, AccountSummary, AccountVerifyResult } from '../../src/lib/api/account'
import { createPhotoFixture, handlePhotoRoute, type PhotoFixture } from './fake-photos'

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
  const base: Account = {
    school_id: 'school-a',
    owner_user_id: 'user-1',
    owner_email_masked: 'o***@example.com',
    name: '',
    display_name: '',
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
  // 与服务端同一口径：name 为空时派生「账号+手机尾 4 位」。
  return {
    ...base,
    display_name:
      overrides.display_name ??
      (base.name.trim() !== '' ? base.name.trim() : `账号${base.phone_masked.slice(-4)}`),
  }
}

/** 完整邮箱形状，与服务端 isCompleteEmailQuery 同形。 */
function isCompleteEmail(value: string): boolean {
  const term = value.trim()
  if (term.split('@').length !== 2 || term.includes('*')) return false
  const [local, domain] = term.split('@')
  return local !== '' && domain !== '' && !/\s/.test(term)
}

/** 三个状态各一条。掩码由登记号 13800138000 / 13900139000 / 13500010001 派生。 */
export const ACCOUNTS: Account[] = [
  account({
    id: 'acc-1',
    phone_masked: '138****8000',
    status: 'pending',
    name: '东区宿舍',
    phone_model: 'Pixel 7',
  }),
  account({
    id: 'acc-2',
    school_id: 'school-b',
    owner_user_id: 'user-2',
    owner_email_masked: 't***@example.com',
    phone_masked: '139****9000',
    status: 'active',
    name: '西区宿舍',
    phone_model: 'iPhone 13',
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

/**
 * 其它页面假 API 的壳摘要夹具。
 * 壳对 master/admin 在所有页面读 `GET /api/accounts/summary`；
 * 不关心计数的页面统一回零，徽标就不渲染。
 */
export const EMPTY_ACCOUNT_SUMMARY: AccountSummary = { pending: 0, rejected: 0 }

/** 归属选择器的合成用户。邮箱是保留域 example.com。 */
export const LOOKUP_USERS = [
  { id: 'user-target', email: 'member.target@example.com', status: 'active' },
  { id: 'user-disabled', email: 'disabled.one@example.com', status: 'disabled' },
  // 同首字符同域对：掩码都是 a***@example.com。
  { id: 'user-a-one', email: 'a.one@example.com', status: 'active' },
  { id: 'user-a-two', email: 'a.two@example.com', status: 'active' },
  // 无点域名：服务端 ValidEmail 允许，UI 不得拦（F3）。
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
  /**
   * 账号 id → 原号。响应只带掩码，改绑判定必须比原号。
   * 缺省取内置登记表；自定义夹具的账号要在这里补原号。
   */
  rawPhones?: Record<string, string>
  /**
   * 属主用户 id → 原邮箱。完整邮箱检索要比原值；缺省取内置登记表。
   * 自定义属主要在 here 补原邮箱，否则完整邮箱精确检索按无地址处理。
   */
  ownerEmails?: Record<string, string>
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
  /** 待处理汇总（/api/accounts/summary）的失败注入。缺省按夹具实时计数。 */
  summaryError?: { status: number; code: string; message: string }
  /** 归属选择器 lookup 的失败注入。 */
  lookupError?: { status: number; code: string; message: string }
  /** 照片夹具。缺省为一份 user-1 拥有两张图的合成库。 */
  photos?: PhotoFixture
}

export type FakeApi = {
  calls: ApiCall[]
  photos: PhotoFixture
  /**
   * 替换列表数据源。轮询用例用它模拟「别人改了数据」：
   * 下一次 10 秒请求带旧 ETag，假 API 回 200 新体。
   */
  setAccounts: (items: Account[]) => void
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-accounts-api' }, trace_id: 'trace-test' },
  })
}

/** 响应体的强校验值：与服务端同口径，取 JSON 字节的 sha256。 */
function etagOf(body: unknown): string {
  return `"${createHash('sha256').update(JSON.stringify(body)).digest('hex')}"`
}

/** 弱比较、逗号列表与 `*` 都认；与真实服务端和浏览器客户端同口径。 */
function ifNoneMatchHit(header: string | undefined, etag: string): boolean {
  if (!header) return false
  return header.split(',').some((candidate) => {
    const value = candidate.trim()
    return value === '*' || value.replace(/^W\//, '') === etag
  })
}

export async function mockAccountsApi(page: Page, options: MockOptions): Promise<FakeApi> {
  const calls: ApiCall[] = []
  const photos = options.photos ?? createPhotoFixture()
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
  // 改绑判定比原号，不比掩码：掩码相同、中四位不同的两个号是两次改绑。
  // 内置夹具的原号来自 tests/synthetic-fixtures.json 的登记段。
  const rawPhones = new Map<string, string>([
    ['acc-1', '13800138000'],
    ['acc-2', '13900139000'],
    ['acc-3', '13500010001'],
    ['acc-del', '13100000000'],
    ...Object.entries(options.rawPhones ?? {}),
  ])
  // 完整邮箱精确检索比原值；列表只展示掩码。
  const ownerEmails = new Map<string, string>([
    ['user-1', options.email ?? SESSION_EMAIL],
    ['user-2', 'target.two@example.com'],
    ...LOOKUP_USERS.map((item) => [item.id, item.email] as [string, string]),
    ...Object.entries(options.ownerEmails ?? {}),
  ])

  /**
   * 与服务端同口径的列表检索：
   * 完整手机号/邮箱精确直达；其余输入按 display_name 子串、可见邮箱掩码、
   * 手机号前 1..3 位或后 4 位发现。5..10 位数字不参与号码匹配。
   */
  const filterAccounts = (items: Account[], query: string): Account[] => {
    const term = query.trim()
    if (term === '') return items
    const lower = term.toLowerCase()
    const phoneShaped = /^[0-9+\-.\s()]+$/.test(term) && /\d/.test(term)
    const digits = phoneShaped ? term.replace(/\D/g, '') : ''
    const normalized =
      digits.length === 13 && digits.startsWith('86') ? digits.slice(2) : digits
    if (phoneShaped && normalized.length === 11) {
      // 完整手机号，或备注名等于**归一后**的 11 位号码（与服务端同口径）。
      return items.filter(
        (item) =>
          rawPhones.get(item.id) === normalized ||
          item.display_name.toLowerCase() === normalized,
      )
    }
    if (isCompleteEmail(term)) {
      // 完整邮箱或同值的备注名：含 @ 的自定义名仍可精确搜到。
      return items.filter(
        (item) =>
          (ownerEmails.get(item.owner_user_id) ?? '').toLowerCase() === lower ||
          item.display_name.toLowerCase() === lower,
      )
    }
    // 可见掩码只保留一个首字符；更长的前缀会暴露隐藏的本地部。
    const mask = (() => {
      if (!term.includes('***')) return null
      const parts = lower.split('***')
      if (parts.length !== 2) return null
      if (Array.from(parts[0]).length !== 1 || !parts[1].startsWith('@')) return null
      return { prefix: parts[0], suffix: parts[1] }
    })()
    return items.filter((item) => {
      if (item.display_name.toLowerCase().includes(lower)) return true
      if (mask) {
        const email = item.owner_email_masked.toLowerCase()
        if (email.startsWith(mask.prefix) && email.endsWith(mask.suffix)) return true
      }
      if (!phoneShaped) return false
      const raw = rawPhones.get(item.id) ?? ''
      if (normalized.length > 0 && normalized.length <= 3) return raw.startsWith(normalized)
      if (normalized.length === 4) return raw.endsWith(normalized)
      return false
    })
  }

  // 只有列表 GET 带校验值：详情与写端点与真服务端一致，不发 ETag。
  const json = (route: Route, request: Request, body: unknown, status = 200) => {
    const conditional =
      request.method() === 'GET' && new URL(request.url()).pathname === '/api/accounts'
    if (!conditional) {
      return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    }
    const etag = etagOf(body)
    if (ifNoneMatchHit(request.headers()['if-none-match'], etag)) {
      return route.fulfill({ status: 304, headers: { ETag: etag } })
    }
    return route.fulfill({
      status,
      contentType: 'application/json',
      headers: { ETag: etag },
      body: JSON.stringify(body),
    })
  }

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

    // 照片域先接管：账号抽屉的图片区与图片字节都走这里。
    if (await handlePhotoRoute(route, request, url, photos, () => sessionRole)) return

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
      return json(route, request, sessionUser(sessionRole))
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
      return json(route, request, sessionUser(sessionRole))
    }

    if (url.pathname === '/api/schools' && request.method() === 'GET') {
      // UX-2 起契约对 user 放宽：三角色都可列学校（user 只见 active 行）。
      return json(route, request, { items: SCHOOLS, next_cursor: '' })
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
      return json(route, request, { items })
    }

    // 建号：成功 201，行并进列表供后续断言。
    if (url.pathname === '/api/accounts' && request.method() === 'POST') {
      if (options.createError) {
        const { status, code, message } = options.createError
        return fail(route, status, code, message)
      }
      const input = (body ?? {}) as {
        school_id?: string
        phone?: string
        name?: string
        owner_user_id?: string
        phone_model?: string
      }
      const owner = LOOKUP_USERS.find((item) => item.id === input.owner_user_id)
      const created = account({
        id: 'acc-new',
        school_id: input.school_id ?? 'school-a',
        owner_user_id: input.owner_user_id ?? 'user-1',
        owner_email_masked: owner
          ? `${owner.email.slice(0, 1)}***${owner.email.slice(owner.email.indexOf('@'))}`
          : 'o***@example.com',
        name: input.name?.trim() ?? '',
        phone_masked: input.phone
          ? `${input.phone.slice(0, 3)}****${input.phone.slice(-4)}`
          : '138****8000',
        // 建号响应与服务端同口径：机型来自刚写入的凭据。
        phone_model: input.phone_model?.trim() ?? '',
      })
      allAccounts.push(created)
      knownAccounts.push(created)
      if (input.phone) rawPhones.set(created.id, input.phone)
      return json(route, request, created, 201)
    }

    if (url.pathname === '/api/accounts' && request.method() === 'GET') {
      const cursor = url.searchParams.get('cursor') ?? ''
      const includeDeleted = url.searchParams.get('include_deleted') === 'true'
      const base = pages[cursor] ?? allAccounts
      // 软删行只在 include_deleted=true 时入选，形状与服务端一致。
      const items = includeDeleted ? [...base, ...deletedAccounts] : base
      const next = options.nextCursors?.[cursor] ?? ''
      const q = url.searchParams.get('q') ?? ''
      return json(route, request, { items: filterAccounts(items, q), next_cursor: next })
    }

    // 待处理账号计数：只数未软删行，从当前夹具实时推导，
    // 写操作（建号/审核/删除/恢复）改列表后计数随之变。
    if (url.pathname === '/api/accounts/summary' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可读账号汇总')
      }
      if (options.summaryError) {
        const { status, code, message } = options.summaryError
        return fail(route, status, code, message)
      }
      const live = [...allAccounts, ...Object.values(pages).flat()]
      return json(route, request, {
        pending: live.filter((item) => item.status === 'pending').length,
        rejected: live.filter((item) => item.status === 'rejected').length,
      })
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
      return json(route, request, found)
    }

    if (!action && request.method() === 'PATCH') {
      const updates = (body ?? {}) as Record<string, unknown>
      if (typeof updates.name === 'string') {
        found.name = updates.name.trim()
      }
      let rebind = false
      if (typeof updates.phone === 'string') {
        // 原号未知时按改绑处理：夹具里没有原号，宁可让用例显性失败，
        // 也不要把「掩码相同」误当成没改号。自定义夹具用 rawPhones 补原号。
        const previous = rawPhones.get(found.id)
        rebind = previous === undefined || previous !== updates.phone
        rawPhones.set(found.id, updates.phone)
        found.phone_masked = `${updates.phone.slice(0, 3)}****${updates.phone.slice(-4)}`
      }
      const credentialEdit =
        typeof updates.password === 'string' ||
        typeof updates.device_code === 'string' ||
        typeof updates.phone_model === 'string'
      // 机型写在凭据里：列表/详情再读时看到的是新值。
      if (typeof updates.phone_model === 'string') {
        found.phone_model = updates.phone_model
      }
      // 与服务端同口径：改绑或改凭据才把被拒账号回待审核；只改备注名不重提。
      // 改绑优先于凭据恢复。停在待审核的账号改绑仍是 pending。
      if (rebind || (found.status === 'rejected' && credentialEdit)) {
        found.status = 'pending'
        found.reject_reason = ''
        found.reviewed_at = null
        found.reviewed_by = null
      } else if (credentialEdit && found.status === 'credential_error') {
        found.status = 'active'
      }
      // 备注名为空时回落到默认名；改绑手机号后尾号也要跟着变。
      found.display_name =
        found.name !== '' ? found.name : `账号${found.phone_masked.slice(-4)}`
      found.updated_at = '2026-10-28T09:30:00Z'
      return json(route, request, found)
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
      return json(route, request, {
        mode: update.mode,
        lng: update.mode === 'fixed' ? update.lng ?? null : null,
        lat: update.mode === 'fixed' ? update.lat ?? null : null,
        reason: update.reason ?? '',
      })
    }

    if (action === 'pause' && request.method() === 'POST') {
      const paused = Boolean((body as { paused?: boolean } | null)?.paused)
      found.status = paused ? 'paused_admin' : 'active'
      return json(route, request, found)
    }
    if (action === 'sign' && request.method() === 'POST') {
      if (options.signError) {
        return fail(route, options.signError.status, options.signError.code, options.signError.message)
      }
      return json(route, request, { task_id: 'task-1' }, 202)
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
      return json(route, request, options.verifyResult ?? { ok: true, code: 'success' })
    }
    if (action === 'review' && request.method() === 'POST') {
      const decision = (body as { decision?: string } | null)?.decision
      // 与服务端同口径：审核推进夹具行的状态，待处理汇总随之变化。
      found.status = decision === 'reject' ? 'rejected' : 'active'
      if (decision === 'reject') found.reject_reason = '合成驳回理由'
      found.reviewed_at = '2026-10-28T09:30:00Z'
      found.reviewed_by = 'user-1'
      return json(route, request, found)
    }

    // 设备刷新：无请求体。回读值缺省即 DEVICE_READ；错误由用例注入。
    if (action === 'device-refresh' && request.method() === 'POST') {
      if (options.deviceRefreshError) {
        const { status, code, message } = options.deviceRefreshError
        return fail(route, status, code, message)
      }
      return json(route, request, DEVICE_READ)
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
      return json(route, request, found)
    }

    return fail(route, 405, 'internal_error', '不支持的方法')
  })

  return {
    calls,
    photos,
    setAccounts(items: Account[]) {
      allAccounts.splice(0, allAccounts.length, ...items.map((item) => ({ ...item })))
      knownAccounts.splice(
        0,
        knownAccounts.length,
        ...allAccounts,
        ...Object.values(pages).flat(),
        ...deletedAccounts,
      )
    },
  }
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

export function summaryCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'GET' && call.path === '/api/accounts/summary',
  )
}

export function changePasswordCalls(api: FakeApi): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === '/api/auth/change-password',
  )
}
