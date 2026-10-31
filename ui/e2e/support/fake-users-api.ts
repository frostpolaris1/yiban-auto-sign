import type { Page, Route } from '@playwright/test'
import type { User } from '../../src/lib/api/user'
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
}

/** 合成用户。四条覆盖三个角色、两种状态与一种已删形状。 */
export const USERS: User[] = [
  {
    id: 'user-alpha',
    email: 'master-one@example.com',
    role: 'master',
    status: 'active',
    must_change_password: false,
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
    created_at: '2026-03-05T00:00:00Z',
    last_login_at: '2026-09-01T02:30:00Z',
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
  created_at: '2026-04-05T00:00:00Z',
  last_login_at: null,
  deleted_at: '2026-10-20T00:00:00Z',
}

/** password-reset 的一次性合成口令。 */
export const FAKE_TEMP_PASSWORD = 'temp-pass-once-0123456789abcdefgh'

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
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-users-api' }, trace_id: 'trace-test' },
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
    })

    if (url.pathname === '/api/auth/me') {
      if (!sessionRole) return fail(route, 401, 'unauthorized', '会话失效')
      return json(route, {
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
      return json(route, { items })
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
      const visible = users.filter((item) => includeDeleted || item.deleted_at === null)
      const cursor = url.searchParams.get('cursor') ?? ''
      const source = options.userPages
        ? (options.userPages[cursor] ?? [])
        : visible
      const next = options.userNextCursors?.[cursor] ?? ''
      return json(route, { items: source, next_cursor: next })
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
        created_at: '2026-10-31T00:00:00Z',
        last_login_at: null,
        deleted_at: null,
      }
      users.push(created)
      return json(route, created, 201)
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
        return json(route, target)
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
        return json(route, target)
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
        return json(route, { temp_password: FAKE_TEMP_PASSWORD })
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

  return { calls }
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
