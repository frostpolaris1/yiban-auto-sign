import type { Page, Route } from '@playwright/test'
import type { School, SchoolAdmin } from '../../src/lib/api/school'
import { SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'

/**
 * 学校页的假 API。
 *
 * 一份路由处理 `/api/auth/me` 与 `/api/schools**`。响应体只用登记过的合成值：
 * 邮箱是保留域 `example.com`，代码是小写合成前缀，名称明示「合成」。
 * 调用记录供断言「请求带哪个参数」与「确认前发没发请求」。
 *
 * 学校与管理员状态按测试各持一份副本：写操作会改状态，不污染共享夹具。
 */

export type ApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

export const SCHOOLS: School[] = [
  {
    id: 'school-alpha',
    code: 'syn-alpha',
    name: '合成学校甲',
    status: 'active',
    timezone: 'Asia/Shanghai',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
  },
  {
    id: 'school-beta',
    code: 'syn-beta',
    name: '合成学校乙',
    status: 'disabled',
    timezone: 'Asia/Shanghai',
    created_at: '2026-01-03T00:00:00Z',
    updated_at: '2026-01-04T00:00:00Z',
  },
]

export const ADMINS: SchoolAdmin[] = [
  {
    user_id: 'user-2',
    email: 'admin-one@example.com',
    role: 'admin',
    granted_by: 'user-1',
    created_at: '2026-02-01T00:00:00Z',
  },
  {
    user_id: 'user-3',
    email: 'admin-two@example.com',
    role: 'master',
    granted_by: 'user-1',
    created_at: '2026-02-02T00:00:00Z',
  },
]

export type MockSchoolsOptions = {
  role: Role | null
  schools?: School[]
  /** 键是本次请求的 cursor；首屏键是空串。 */
  schoolPages?: Record<string, School[]>
  schoolNextCursors?: Record<string, string>
  /** 学校 ID → cursor → 管理员页。缺省用 `ADMINS`。 */
  adminPages?: Record<string, Record<string, SchoolAdmin[]>>
  adminNextCursors?: Record<string, Record<string, string>>
  listError?: { status: number; code: string; message: string }
  createError?: { status: number; code: string; message: string }
}

export type FakeSchoolsApi = {
  calls: ApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-schools-api' }, trace_id: 'trace-test' },
  })
}

export async function mockSchoolsApi(
  page: Page,
  options: MockSchoolsOptions,
): Promise<FakeSchoolsApi> {
  const calls: ApiCall[] = []
  const sessionRole = options.role
  const schools = (options.schools ?? SCHOOLS).map((item) => ({ ...item }))
  const adminsBySchool: Record<string, SchoolAdmin[]> = {
    'school-alpha': ADMINS.map((item) => ({ ...item })),
    'school-beta': [],
  }
  const schoolPages = Object.fromEntries(
    Object.entries(options.schoolPages ?? {}).map(([cursor, items]) => [
      cursor,
      items.map((item) => ({ ...item })),
    ]),
  )
  const adminPages = Object.fromEntries(
    Object.entries(options.adminPages ?? {}).map(([schoolId, pages]) => [
      schoolId,
      Object.fromEntries(
        Object.entries(pages).map(([cursor, items]) => [cursor, items.map((item) => ({ ...item }))]),
      ),
    ]),
  )

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })

  const requireManager = async (route: Route): Promise<boolean> => {
    if (sessionRole === 'master' || sessionRole === 'admin') return true
    await fail(route, 403, 'forbidden', '无权访问')
    return false
  }

  const requireMaster = async (route: Route): Promise<boolean> => {
    if (sessionRole === 'master') return true
    await fail(route, 403, 'forbidden', '仅 master 可执行此操作')
    return false
  }

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

    if (url.pathname === '/api/schools' && request.method() === 'GET') {
      if (!(await requireManager(route))) return
      if (options.listError) {
        return fail(
          route,
          options.listError.status,
          options.listError.code,
          options.listError.message,
        )
      }
      const cursor = url.searchParams.get('cursor') ?? ''
      const items = options.schoolPages ? (schoolPages[cursor] ?? []) : schools
      const next = options.schoolNextCursors?.[cursor] ?? ''
      return json(route, { items, next_cursor: next })
    }

    if (url.pathname === '/api/schools' && request.method() === 'POST') {
      if (!(await requireMaster(route))) return
      if (options.createError) {
        return fail(
          route,
          options.createError.status,
          options.createError.code,
          options.createError.message,
        )
      }
      const payload = (body ?? {}) as { code: string; name: string; timezone: string }
      const created: School = {
        id: `school-${schools.length + 1}`,
        code: payload.code,
        name: payload.name,
        status: 'active',
        timezone: payload.timezone,
        created_at: '2026-10-28T09:00:00Z',
        updated_at: '2026-10-28T09:00:00Z',
      }
      schools.push(created)
      return json(route, created, 201)
    }

    const detail = url.pathname.match(/^\/api\/schools\/([^/]+)$/)
    if (detail && request.method() === 'PATCH') {
      if (!(await requireMaster(route))) return
      const found = schools.find((item) => item.id === detail[1])
      if (!found) return fail(route, 404, 'not_found', '学校不存在')
      const updates = (body ?? {}) as Partial<Pick<School, 'name' | 'status' | 'timezone'>>
      if (updates.name !== undefined) found.name = updates.name
      if (updates.status !== undefined) found.status = updates.status
      if (updates.timezone !== undefined) found.timezone = updates.timezone
      found.updated_at = '2026-10-28T10:00:00Z'
      return json(route, found)
    }

    const admins = url.pathname.match(/^\/api\/schools\/([^/]+)\/admins$/)
    if (admins) {
      const schoolId = admins[1]
      if (request.method() === 'GET') {
        if (!(await requireMaster(route))) return
        const cursor = url.searchParams.get('cursor') ?? ''
        const pages = adminPages[schoolId]
        const items = pages ? (pages[cursor] ?? []) : (adminsBySchool[schoolId] ?? [])
        const next = options.adminNextCursors?.[schoolId]?.[cursor] ?? ''
        return json(route, { items, next_cursor: next })
      }
      if (request.method() === 'POST') {
        if (!(await requireMaster(route))) return
        const payload = (body ?? {}) as { user_id?: string }
        const userId = (payload.user_id ?? '').trim()
        if (userId === '') return fail(route, 400, 'validation_error', 'user_id 必填')
        const created: SchoolAdmin = {
          user_id: userId,
          email: `${userId}@example.com`,
          role: 'admin',
          granted_by: 'user-1',
          created_at: '2026-10-28T10:30:00Z',
        }
        adminsBySchool[schoolId] = [...(adminsBySchool[schoolId] ?? []), created]
        return json(route, created, 201)
      }
    }

    const member = url.pathname.match(/^\/api\/schools\/([^/]+)\/admins\/([^/]+)$/)
    if (member && request.method() === 'DELETE') {
      if (!(await requireMaster(route))) return
      const [, schoolId, userId] = member
      adminsBySchool[schoolId] = (adminsBySchool[schoolId] ?? []).filter(
        (item) => item.user_id !== userId,
      )
      return route.fulfill({ status: 204 })
    }

    return fail(route, 404, 'not_found', '未找到资源')
  })

  return { calls }
}

export function listSchoolCalls(api: FakeSchoolsApi): ApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/schools')
}

export function lastSchoolListCall(api: FakeSchoolsApi): ApiCall | undefined {
  return listSchoolCalls(api).at(-1)
}

export function createSchoolCalls(api: FakeSchoolsApi): ApiCall[] {
  return api.calls.filter((call) => call.method === 'POST' && call.path === '/api/schools')
}

export function patchSchoolCalls(api: FakeSchoolsApi, id: string): ApiCall[] {
  return api.calls.filter((call) => call.method === 'PATCH' && call.path === `/api/schools/${id}`)
}

export function adminListCalls(api: FakeSchoolsApi, id: string): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'GET' && call.path === `/api/schools/${id}/admins`,
  )
}

export function adminAddCalls(api: FakeSchoolsApi, id: string): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === `/api/schools/${id}/admins`,
  )
}

export function adminRemoveCalls(api: FakeSchoolsApi, id: string, uid: string): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'DELETE' && call.path === `/api/schools/${id}/admins/${uid}`,
  )
}
