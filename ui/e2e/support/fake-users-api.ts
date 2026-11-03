import { createHash } from 'node:crypto'
import type { Page, Request, Route } from '@playwright/test'
import type { User, UserCounts } from '../../src/lib/api/user'
import { SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'

/**
 * 用户管理页的假 API。
 *
 * 一份路由处理 `/api/auth/me` 与 `/api/users**`。
 * 响应体只用登记过的合成值：邮箱是保留域 `example.com`，前缀明示合成口径。
 * 调用记录供断言「请求带哪个参数」与「无权限时发没发请求」。
 *
 * `GET /api/users` 只归 master：其余角色返回 403 信封。
 * 页对象形状与契约一致：`{ items, next_cursor }`，末页 `next_cursor` 是空串。
 * UX-1 的写端点在本假 API 内改内存副本：PATCH 改角色/状态、
 * password-reset 发一次性合成口令、DELETE 打软删标记、restore 清掉，
 * lookup 只回 id、掩码邮箱、状态。`deleted_at` 非空的行只在
 * `include_deleted=true` 时出现。
 */

export type ApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
  headers: Record<string, string>
}

/** 合成用户。四条覆盖三个角色、两种状态与一种已删形状。 */
export const USERS: User[] = [
  {
    id: 'user-alpha',
    email: 'master-one@example.com',
    role: 'master',
    status: 'active',
    must_change_password: false,
    account_count: 3,
    pending_count: 0,
    review_count: 0,
    created_at: '2026-01-05T00:00:00Z',
    last_login_at: '2026-10-28T01:00:00Z',
    deleted_at: null,
  },
  {
    id: 'user-beta',
    email: 'admin-one@example.com',
    role: 'admin',
    status: 'active',
    must_change_password: true,
    account_count: 1,
    pending_count: 0,
    review_count: 0,
    created_at: '2026-02-05T00:00:00Z',
    last_login_at: null,
    deleted_at: null,
  },
  {
    id: 'user-gamma',
    email: 'member-one@example.com',
    role: 'user',
    status: 'disabled',
    must_change_password: false,
    account_count: 0,
    pending_count: 0,
    review_count: 0,
    created_at: '2026-03-05T00:00:00Z',
    last_login_at: '2026-09-01T02:30:00Z',
    deleted_at: null,
  },
  {
    id: 'user-epsilon',
    email: 'member-two@example.com',
    role: 'user',
    status: 'active',
    must_change_password: false,
    account_count: 2,
    pending_count: 1,
    review_count: 1,
    created_at: '2026-04-05T00:00:00Z',
    last_login_at: null,
    deleted_at: null,
  },
]

/** 软删行夹具：只在 include_deleted=true 时出现。 */
export const DELETED_USER: User = {
  id: 'user-delta',
  email: 'gone-one@example.com',
  role: 'user',
  status: 'disabled',
  must_change_password: false,
  account_count: 0,
  pending_count: 0,
  review_count: 0,
  created_at: '2026-05-05T00:00:00Z',
  last_login_at: null,
  deleted_at: '2026-10-20T00:00:00Z',
}

/** password-reset 的一次性合成口令。 */
export const FAKE_TEMP_PASSWORD = 'temp-pass-once-0123456789abcdefgh'

/** 与服务端同口径：review>0 → pending；无账号 → vacant；否则 normal。 */
function groupOf(user: User): 'pending' | 'normal' | 'vacant' {
  if (user.review_count > 0) return 'pending'
  return user.account_count === 0 ? 'vacant' : 'normal'
}

/** 全局分组计数：前三组只数未删用户，deleted 只数软删用户。 */
function userCountsOf(items: User[]): UserCounts {
  const counts: UserCounts = { pending: 0, normal: 0, vacant: 0, deleted: 0 }
  for (const item of items) {
    if (item.deleted_at !== null) {
      counts.deleted += 1
      continue
    }
    counts[groupOf(item)] += 1
  }
  return counts
}

export type MockUsersOptions = {
  role: Role | null
  /** 单页列表。给了 `userPages` 时忽略本项。 */
  users?: User[]
  /** 键是本次请求的 cursor；首屏键是空串。 */
  userPages?: Record<string, User[]>
  userNextCursors?: Record<string, string>
  listError?: { status: number; code: string; message: string }
  createError?: { status: number; code: string; message: string }
  updateError?: { status: number; code: string; message: string }
  resetError?: { status: number; code: string; message: string }
  deleteError?: { status: number; code: string; message: string }
  restoreError?: { status: number; code: string; message: string }
  lookupError?: { status: number; code: string; message: string }
}

export type FakeUsersApi = {
  calls: ApiCall[]
  /**
   * 替换列表数据源。轮询用例用它模拟「别人改了数据」：
   * 下一次 10 秒请求带旧 ETag，假 API 回 200 新体。
   */
  setUsers: (items: User[]) => void
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-users-api' }, trace_id: 'trace-test' },
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

function maskEmail(email: string): string {
  const at = email.indexOf('@')
  if (at <= 0) return email
  return `${email.slice(0, 1)}***${email.slice(at)}`
}

export async function mockUsersApi(
  page: Page,
  options: MockUsersOptions,
): Promise<FakeUsersApi> {
  const calls: ApiCall[] = []
  const sessionRole = options.role
  // deleted 行默认不在列表；include_deleted 时并入。
  const users = (options.users ?? [...USERS, DELETED_USER]).map((item) => ({ ...item }))

  const json = (route: Route, request: Request, body: unknown, status = 200) => {
    const etag = etagOf(body)
    if (request.method() === 'GET' && ifNoneMatchHit(request.headers()['if-none-match'], etag)) {
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

    if (url.pathname === '/api/auth/me') {
      if (!sessionRole) return fail(route, 401, 'unauthorized', '会话失效')
      return json(route, request, {
        id: 'user-1',
        email: SESSION_EMAIL,
        role: sessionRole,
        status: 'active',
        must_change_password: false,
        csrf_token: SESSION_CSRF,
      })
    }

    if (url.pathname === '/api/users/lookup' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可检索用户')
      }
      if (options.lookupError) {
        return fail(
          route,
          options.lookupError.status,
          options.lookupError.code,
          options.lookupError.message,
        )
      }
      const q = (url.searchParams.get('q') ?? '').toLowerCase()
      const items = users
        .filter((item) => item.deleted_at === null && item.email.startsWith(q))
        .slice(0, Number(url.searchParams.get('limit') ?? 20))
        .map((item) => ({
          id: item.id,
          email_masked: maskEmail(item.email),
          status: item.status,
        }))
      return json(route, request, { items })
    }

    if (url.pathname === '/api/users' && request.method() === 'GET') {
      if (sessionRole !== 'master') {
        return fail(route, 403, 'forbidden', '仅 master 可查看用户列表')
      }
      if (options.listError) {
        return fail(
          route,
          options.listError.status,
          options.listError.code,
          options.listError.message,
        )
      }
      const includeDeleted = url.searchParams.get('include_deleted') === 'true'
      const group = url.searchParams.get('group') ?? ''
      if (group !== '' && !['pending', 'normal', 'vacant'].includes(group)) {
        return fail(route, 400, 'validation_failed', '未知用户分组')
      }
      if (group !== '' && includeDeleted) {
        return fail(route, 400, 'validation_failed', 'group 不能与 include_deleted 组合')
      }
      const visible = users.filter((item) => {
        if (group !== '') {
          return item.deleted_at === null && groupOf(item) === group
        }
        return includeDeleted || item.deleted_at === null
      })
      const cursor = url.searchParams.get('cursor') ?? ''
      // userPages 是按游标预排的页；它们也要吃同一套 group/include_deleted
      // 过滤，否则分组请求会读到视图外的行。过滤后为空的页不发下一页
      // 游标：翻页语义不得越过当前视图。
      const source = options.userPages
        ? (options.userPages[cursor] ?? []).filter((item) => {
            if (group !== '') {
              return item.deleted_at === null && groupOf(item) === group
            }
            return includeDeleted || item.deleted_at === null
          })
        : visible
      let next = options.userNextCursors?.[cursor] ?? ''
      if (options.userPages && source.length === 0) next = ''
      return json(route, request, {
        items: source,
        next_cursor: next,
        counts: userCountsOf(users),
      })
    }

    if (url.pathname === '/api/users' && request.method() === 'POST') {
      if (options.createError) {
        return fail(
          route,
          options.createError.status,
          options.createError.code,
          options.createError.message,
        )
      }
      const input = body as { email: string; role: User['role'] }
      const created: User = {
        id: 'user-new',
        email: input.email,
        role: input.role,
        status: 'active',
        must_change_password: false,
        account_count: 0,
        pending_count: 0,
        review_count: 0,
        created_at: '2026-10-31T00:00:00Z',
        last_login_at: null,
        deleted_at: null,
      }
      users.push(created)
      return json(route, request, created, 201)
    }

    const userMatch = /^\/api\/users\/([^/]+)(\/(password-reset|restore))?$/.exec(url.pathname)
    if (userMatch) {
      const id = userMatch[1]
      const action = userMatch[3] ?? ''
      const target = users.find((item) => item.id === id)
      if (!target) {
        return fail(route, 404, 'not_found', '用户不存在')
      }
      if (request.method() === 'POST' && action === 'restore') {
        if (options.restoreError) {
          return fail(
            route,
            options.restoreError.status,
            options.restoreError.code,
            options.restoreError.message,
          )
        }
        // 恢复只对已删行有效：服务端对未删行同判 404。
        if (target.deleted_at === null) {
          return fail(route, 404, 'not_found', '用户未删除')
        }
        target.deleted_at = null
        return json(route, request, target)
      }
      // PATCH / reset / delete 都不读软删行。
      if (target.deleted_at !== null) {
        return fail(route, 404, 'not_found', '用户已删除')
      }
      if (request.method() === 'PATCH') {
        if (options.updateError) {
          return fail(
            route,
            options.updateError.status,
            options.updateError.code,
            options.updateError.message,
          )
        }
        const patch = body as { role?: User['role']; status?: User['status'] }
        if (patch.role) target.role = patch.role
        if (patch.status) target.status = patch.status
        return json(route, request, target)
      }
      if (request.method() === 'POST' && action === 'password-reset') {
        if (options.resetError) {
          return fail(
            route,
            options.resetError.status,
            options.resetError.code,
            options.resetError.message,
          )
        }
        target.must_change_password = true
        return json(route, request, { temp_password: FAKE_TEMP_PASSWORD })
      }
      if (request.method() === 'DELETE') {
        if (options.deleteError) {
          return fail(
            route,
            options.deleteError.status,
            options.deleteError.code,
            options.deleteError.message,
          )
        }
        target.deleted_at = '2026-10-31T00:00:00Z'
        return route.fulfill({ status: 204 })
      }
    }

    return fail(route, 404, 'not_found', '未找到资源')
  })

  return {
    calls,
    setUsers(items: User[]) {
      users.splice(0, users.length, ...items.map((item) => ({ ...item })))
    },
  }
}

export function listUserCalls(api: FakeUsersApi): ApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/users')
}

export function lastUserListCall(api: FakeUsersApi): ApiCall | undefined {
  return listUserCalls(api).at(-1)
}

export function callsTo(api: FakeUsersApi, method: string, path: string): ApiCall[] {
  return api.calls.filter((call) => call.method === method && call.path === path)
}
