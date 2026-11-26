import type { Page, Route } from '@playwright/test'
import type { AuditLogItem, AuditStatusReport } from '../../src/lib/api/audit'
import { SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'
import { handleDefaultNotifyRoute } from './fake-notify-api'

/**
 * 审计日志页（/audit）的假 API：会话、链巡检与游标台账。
 *
 * 一份路由处理 `/api/auth/me`、`/api/admin/audit-status`、`/api/audit-logs`
 * 与侧栏共用的 `/api/accounts/summary`（回零）。
 * 响应体只用登记过的合成值：操作者是保留域邮箱掩码，对象是合成 id。
 * 台账 master/admin 可读（admin 的行由服务端按绑定校收窄）；巡检仅 master。
 * 调用记录供断言「默认参数」「游标只前进」「无权限零请求」。
 * 错误注入默认每次都失败；`once: true` 时只有第一次失败，用来验「重试」。
 */

export type AuditApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

export type AuditFailure = {
  status: number
  code: string
  message: string
  /** 只让第一次请求失败；之后成功。 */
  once?: boolean
}

/** 完好链缺省结论：固定行数与时刻，视觉基线不随日历漂移。 */
export const AUDIT_STATUS_OK: AuditStatusReport = {
  ok: true,
  empty: false,
  checked_rows: 128,
  last_hash: 'ab'.repeat(32),
  broken_at: null,
  checked_at: '2026-11-10T09:00:00+08:00',
}

/** 断链结论：第 4 行不符，供页头危险告警用例。 */
export const AUDIT_STATUS_BROKEN: AuditStatusReport = {
  ok: false,
  empty: false,
  checked_rows: 3,
  last_hash: 'cd'.repeat(32),
  broken_at: 4,
  checked_at: '2026-11-10T09:05:00+08:00',
  anchor_status: 'not_checked',
}

/** 六行台账：时间倒序，覆盖常见动作、空详情与长详情。 */
export const AUDIT_LOGS: AuditLogItem[] = [
  {
    id: 9006,
    at: '2026-11-10T09:30:00+08:00',
    action: 'auth.login',
    actor: 'm***@example.com',
    target: 'user-1',
    detail: '{"result":"ok","ip":"eg-demo-1"}',
  },
  {
    id: 9005,
    at: '2026-11-10T09:12:00+08:00',
    action: 'account.review',
    actor: 'o***@example.com',
    target: 'acc-0002',
    detail: '{"decision":"approve","reason":""}',
  },
  {
    id: 9004,
    at: '2026-11-10T08:58:00+08:00',
    action: 'task.rearm',
    actor: 'o***@example.com',
    target: 'task-demo-1',
    detail: '{"attempts":2,"result":"transient"}',
  },
  {
    id: 9003,
    at: '2026-11-10T08:41:00+08:00',
    action: 'settings.engine_paused',
    actor: 'm***@example.com',
    target: 'engine',
    detail: '{"paused":true}',
  },
  {
    id: 9002,
    at: '2026-11-10T08:30:00+08:00',
    action: 'school.default_photo_set',
    actor: 'm***@example.com',
    target: 'school-a',
    detail: '{"photo_id":"photo-demo-gate"}',
  },
  {
    id: 9001,
    at: '2026-11-10T08:12:00+08:00',
    action: 'auth.logout',
    actor: 'a***@example.com',
    target: 'user-9',
    detail: '',
  },
]

export type MockAuditOptions = {
  role: Role | null
  /** 不配置 pages 时的整页夹具。 */
  logs?: AuditLogItem[]
  /** 按游标分页：如 { '': [...], 'c2': [...] }。 */
  pages?: Record<string, AuditLogItem[]>
  /** 按游标给下一页游标；缺省末页（空串）。 */
  nextCursors?: Record<string, string>
  /** total 快照；缺省等于本页条数。 */
  total?: number
  status?: AuditStatusReport
  statusError?: AuditFailure
  listError?: AuditFailure
}

export type FakeAuditApi = {
  calls: AuditApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-audit-api' }, trace_id: 'trace-audit' },
  })
}

type FailureEntry = { spec: AuditFailure | undefined; hits: number }

export async function mockAuditApi(
  page: Page,
  options: MockAuditOptions,
): Promise<FakeAuditApi> {
  const calls: AuditApiCall[] = []
  const sessionRole = options.role
  const logs: AuditLogItem[] = (options.logs ?? AUDIT_LOGS).map((item) => ({ ...item }))
  const status: AuditStatusReport = { ...(options.status ?? AUDIT_STATUS_OK) }
  const failures: Record<'list' | 'status', FailureEntry> = {
    list: { spec: options.listError, hits: 0 },
    status: { spec: options.statusError, hits: 0 },
  }

  const json = (route: Route, body: unknown, statusCode = 200) =>
    route.fulfill({ status: statusCode, contentType: 'application/json', body: JSON.stringify(body) })

  const fail = (route: Route, statusCode: number, code: string, message: string) =>
    route.fulfill({
      status: statusCode,
      contentType: 'application/json',
      body: errorBody(code, message),
    })

  const takeFailure = (key: keyof typeof failures): AuditFailure | null => {
    const entry = failures[key]
    if (!entry.spec) return null
    entry.hits += 1
    if (entry.spec.once && entry.hits > 1) return null
    return entry.spec
  }

  const requireAdmin = async (route: Route): Promise<boolean> => {
    if (sessionRole === 'master' || sessionRole === 'admin') return true
    await fail(route, 403, 'forbidden', 'master 或 admin 角色才可读取审计')
    return false
  }

  const requireMaster = async (route: Route): Promise<boolean> => {
    if (sessionRole === 'master') return true
    await fail(route, 403, 'forbidden', '仅 master 可巡检审计链')
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

    if (await handleDefaultNotifyRoute(route, request, url, sessionRole)) return

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

    if (url.pathname === '/api/admin/audit-status' && request.method() === 'GET') {
      if (!(await requireMaster(route))) return
      const failure = takeFailure('status')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      return json(route, status)
    }

    if (url.pathname === '/api/audit-logs' && request.method() === 'GET') {
      if (!(await requireAdmin(route))) return
      const failure = takeFailure('list')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      const cursor = url.searchParams.get('cursor') ?? ''
      const items = options.pages ? (options.pages[cursor] ?? []) : logs
      const nextCursor = options.nextCursors?.[cursor] ?? ''
      return json(route, {
        items,
        next_cursor: nextCursor,
        total: options.total ?? items.length,
        has_more: nextCursor !== '',
      })
    }

    // 壳对 master/admin 读待处理计数；审计用例不关心徽标，统一回零。
    if (url.pathname === '/api/accounts/summary' && request.method() === 'GET') {
      return json(route, { pending: 0, rejected: 0 })
    }

    return fail(route, 404, 'not_found', `fake-audit-api 未登记 ${url.pathname}`)
  })

  return { calls }
}

export function auditListCalls(api: FakeAuditApi): AuditApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/audit-logs')
}

export function auditStatusCalls(api: FakeAuditApi): AuditApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'GET' && call.path === '/api/admin/audit-status',
  )
}
