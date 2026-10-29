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
 */

export type ApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

/** 合成用户。三条恰好覆盖三个角色与两种状态，含空的上次登录。 */
export const USERS: User[] = [
  {
    id: 'user-alpha',
    email: 'master-one@example.com',
    role: 'master',
    status: 'active',
    must_change_password: false,
    created_at: '2026-01-05T00:00:00Z',
    last_login_at: '2026-10-28T01:00:00Z',
  },
  {
    id: 'user-beta',
    email: 'admin-one@example.com',
    role: 'admin',
    status: 'active',
    must_change_password: true,
    created_at: '2026-02-05T00:00:00Z',
    last_login_at: null,
  },
  {
    id: 'user-gamma',
    email: 'member-one@example.com',
    role: 'user',
    status: 'disabled',
    must_change_password: false,
    created_at: '2026-03-05T00:00:00Z',
    last_login_at: '2026-09-01T02:30:00Z',
  },
]

export type MockUsersOptions = {
  role: Role | null
  /** 单页列表。给了 `userPages` 时忽略本项。 */
  users?: User[]
  /** 键是本次请求的 cursor；首屏键是空串。 */
  userPages?: Record<string, User[]>
  userNextCursors?: Record<string, string>
  listError?: { status: number; code: string; message: string }
}

export type FakeUsersApi = {
  calls: ApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-users-api' }, trace_id: 'trace-test' },
  })
}

export async function mockUsersApi(
  page: Page,
  options: MockUsersOptions,
): Promise<FakeUsersApi> {
  const calls: ApiCall[] = []
  const sessionRole = options.role
  const users = (options.users ?? USERS).map((item) => ({ ...item }))
  const userPages = Object.fromEntries(
    Object.entries(options.userPages ?? {}).map(([cursor, items]) => [
      cursor,
      items.map((item) => ({ ...item })),
    ]),
  )

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
      const cursor = url.searchParams.get('cursor') ?? ''
      const items = options.userPages ? (userPages[cursor] ?? []) : users
      const next = options.userNextCursors?.[cursor] ?? ''
      return json(route, { items, next_cursor: next })
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
