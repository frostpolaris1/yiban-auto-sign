import type { Page, Route } from '@playwright/test'
import type { Account, AccountSummary } from '../../src/lib/api/account'
import type { ScheduleWindow } from '../../src/lib/api/schedule'
import type { AccountDiagnostics, AdminTaskRow } from '../../src/lib/api/task'
import { SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'
import { RUN_DIAGNOSTICS } from './fake-run-api'
import { SCHOOLS } from './fake-schools-api'

/**
 * 待办页（/queue）的假 API：组合账号审核、任务列表、窗口、学校与诊断。
 * 所有值均为登记合成夹具，保留完整请求日志以便验证各组游标互不串扰。
 */

export type QueueApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

export type QueueFailure = {
  status: number
  code: string
  message: string
}

export const QUEUE_ACCOUNTS: Account[] = [
  {
    id: 'q-acc-1',
    school_id: 'school-alpha',
    owner_user_id: 'user-1',
    owner_email_masked: 'o***@example.com',
    name: '东区宿舍',
    display_name: '东区宿舍',
    phone_masked: '138****8000',
    status: 'pending',
    reject_reason: '',
    reviewed_at: null,
    reviewed_by: null,
    created_at: '2026-10-31T08:00:00+08:00',
    updated_at: '2026-10-31T08:00:00+08:00',
    deleted_at: null,
  },
  {
    id: 'q-acc-2',
    school_id: 'school-beta',
    owner_user_id: 'user-2',
    owner_email_masked: 't***@example.com',
    name: '南区教学楼',
    display_name: '南区教学楼',
    phone_masked: '139****9000',
    status: 'pending',
    reject_reason: '',
    reviewed_at: null,
    reviewed_by: null,
    created_at: '2026-10-31T08:05:00+08:00',
    updated_at: '2026-10-31T08:05:00+08:00',
    deleted_at: null,
  },
  {
    id: 'q-acc-3',
    school_id: 'school-alpha',
    owner_user_id: 'user-3',
    owner_email_masked: 'a***@example.com',
    name: '图书馆',
    display_name: '图书馆',
    phone_masked: '135****0001',
    status: 'rejected',
    reject_reason: '所属学校不匹配',
    reviewed_at: '2026-10-30T10:00:00+08:00',
    reviewed_by: 'user-1',
    created_at: '2026-10-30T09:00:00+08:00',
    updated_at: '2026-10-30T10:00:00+08:00',
    deleted_at: null,
  },
]

export const QUEUE_TASKS: AdminTaskRow[] = [
  {
    id: 'q-task-fail-1',
    account_id: 'q-acc-fail-1',
    account_phone_masked: '138****8003',
    school_id: 'school-alpha',
    school_name: '合成学校甲',
    task_name: '早签到',
    state: 'failed',
    attempts: 3,
    result: 'credential_invalid',
    run_at: '2026-10-31T08:30:00+08:00',
    claimed_at: '2026-10-31T08:30:00+08:00',
    updated_at: '2026-10-31T08:36:00+08:00',
  },
  {
    id: 'q-task-pending-1',
    account_id: 'q-acc-pend-1',
    account_phone_masked: '138****8004',
    school_id: 'school-beta',
    school_name: '合成学校乙',
    task_name: '晚自习签到',
    state: 'pending',
    attempts: 0,
    result: '',
    run_at: '2026-10-31T09:00:00+08:00',
    claimed_at: null,
    updated_at: '2026-10-31T08:40:00+08:00',
  },
  {
    id: 'q-task-run-1',
    account_id: 'q-acc-run-1',
    account_phone_masked: '138****8001',
    school_id: 'school-alpha',
    school_name: '合成学校甲',
    task_name: '早签到',
    state: 'running',
    attempts: 1,
    result: '',
    run_at: '2026-10-31T08:10:00+08:00',
    claimed_at: '2026-10-31T08:10:00+08:00',
    updated_at: '2026-10-31T08:12:00+08:00',
  },
]

export const QUEUE_WINDOWS: ScheduleWindow[] = [
  {
    school_id: 'school-alpha',
    school_name: '合成学校甲',
    task_id: 'task-morning',
    task_name: '早签到',
    enabled: true,
    day: '2026-10-31',
    start: '2026-10-31T08:30:00+08:00',
    end: '2026-10-31T09:00:00+08:00',
    source: 'cache',
    version: 2,
  },
]

export type MockQueueOptions = {
  role: Role | null
  accounts?: Account[]
  tasks?: AdminTaskRow[]
  windows?: ScheduleWindow[]
  /** 按 status + cursor 分页：如 { 'pending:': [...], 'pending:c1': [...] } */
  accountPages?: Record<string, Account[]>
  accountNextCursors?: Record<string, string>
  /** 按 state + cursor 分页：如 { 'failed:': [...], 'failed:f1': [...] } */
  taskPages?: Record<string, AdminTaskRow[]>
  taskNextCursors?: Record<string, string>
  accountsError?: QueueFailure
  tasksError?: QueueFailure
  windowsError?: QueueFailure
  reviewError?: QueueFailure
  signError?: QueueFailure
  diagnosticsError?: QueueFailure
  diagnostics?: AccountDiagnostics
}

export type FakeQueueApi = {
  calls: QueueApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-queue-api' }, trace_id: 'trace-queue' },
  })
}

export async function mockQueueApi(
  page: Page,
  options: MockQueueOptions,
): Promise<FakeQueueApi> {
  const calls: QueueApiCall[] = []
  const sessionRole = options.role
  const accounts: Account[] = (options.accounts ?? QUEUE_ACCOUNTS).map((item) => ({ ...item }))
  const tasks: AdminTaskRow[] = (options.tasks ?? QUEUE_TASKS).map((item) => ({ ...item }))
  const windows: ScheduleWindow[] = options.windows ?? QUEUE_WINDOWS

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let parsedBody: unknown = null
    const rawBody = request.postData()
    if (rawBody) {
      try {
        parsedBody = JSON.parse(rawBody)
      } catch {
        parsedBody = rawBody
      }
    }

    calls.push({
      method: request.method(),
      path: url.pathname,
      search: url.search,
      params: Object.fromEntries(url.searchParams.entries()),
      body: parsedBody,
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

    if (url.pathname === '/api/accounts/summary' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可读汇总')
      }
      const summary: AccountSummary = {
        pending: accounts.filter((a) => a.status === 'pending' && !a.deleted_at).length,
        rejected: accounts.filter((a) => a.status === 'rejected' && !a.deleted_at).length,
      }
      return json(route, summary)
    }

    if (url.pathname === '/api/schools' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '无权限')
      }
      return json(route, { items: SCHOOLS, next_cursor: '' })
    }

    if (url.pathname === '/api/schedule/windows' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '无权限')
      }
      if (options.windowsError) {
        return fail(
          route,
          options.windowsError.status,
          options.windowsError.code,
          options.windowsError.message,
        )
      }
      return json(route, {
        day: url.searchParams.get('day') ?? '2026-10-31',
        items: windows,
      })
    }

    if (url.pathname === '/api/accounts' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '无权限')
      }
      if (options.accountsError) {
        return fail(
          route,
          options.accountsError.status,
          options.accountsError.code,
          options.accountsError.message,
        )
      }
      const status = url.searchParams.get('status') ?? ''
      const cursor = url.searchParams.get('cursor') ?? ''
      const key = `${status}:${cursor}`
      if (options.accountPages) {
        return json(route, {
          items: options.accountPages[key] ?? [],
          next_cursor: options.accountNextCursors?.[key] ?? '',
        })
      }
      const filtered = status ? accounts.filter((a) => a.status === status) : accounts
      return json(route, { items: filtered, next_cursor: '' })
    }

    if (
      url.pathname.startsWith('/api/accounts/') &&
      url.pathname.endsWith('/review') &&
      request.method() === 'POST'
    ) {
      if (options.reviewError) {
        return fail(
          route,
          options.reviewError.status,
          options.reviewError.code,
          options.reviewError.message,
        )
      }
      const id = decodeURIComponent(url.pathname.split('/')[3] ?? '')
      const target = accounts.find((a) => a.id === id)
      if (!target) return fail(route, 404, 'not_found', '账号不存在')
      const body = (parsedBody ?? {}) as { decision?: 'approve' | 'reject'; reason?: string }
      if (body.decision === 'approve') {
        target.status = 'active'
        target.reject_reason = ''
      } else if (body.decision === 'reject') {
        target.status = 'rejected'
        target.reject_reason = body.reason ?? ''
      }
      target.reviewed_at = '2026-10-31T09:00:00+08:00'
      return json(route, target)
    }

    if (
      url.pathname.startsWith('/api/accounts/') &&
      url.pathname.endsWith('/sign') &&
      request.method() === 'POST'
    ) {
      if (options.signError) {
        return fail(
          route,
          options.signError.status,
          options.signError.code,
          options.signError.message,
        )
      }
      return json(route, { task_id: 'task-retry-accepted' }, 202)
    }

    if (
      url.pathname.startsWith('/api/accounts/') &&
      url.pathname.endsWith('/diagnostics') &&
      request.method() === 'GET'
    ) {
      if (options.diagnosticsError) {
        return fail(
          route,
          options.diagnosticsError.status,
          options.diagnosticsError.code,
          options.diagnosticsError.message,
        )
      }
      return json(route, options.diagnostics ?? RUN_DIAGNOSTICS)
    }

    if (url.pathname === '/api/admin/tasks' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '无权限')
      }
      if (options.tasksError) {
        return fail(
          route,
          options.tasksError.status,
          options.tasksError.code,
          options.tasksError.message,
        )
      }
      const state = url.searchParams.get('state') ?? ''
      const cursor = url.searchParams.get('cursor') ?? ''
      const key = `${state}:${cursor}`
      if (options.taskPages) {
        return json(route, {
          items: options.taskPages[key] ?? [],
          next_cursor: options.taskNextCursors?.[key] ?? '',
        })
      }
      const filtered = state ? tasks.filter((t) => t.state === state) : tasks
      return json(route, { items: filtered, next_cursor: '' })
    }

    return fail(route, 404, 'not_found', `fake-queue-api 未登记 ${url.pathname}`)
  })

  return { calls }
}

export function accountListCalls(api: FakeQueueApi): QueueApiCall[] {
  return api.calls.filter((c) => c.path === '/api/accounts' && c.method === 'GET')
}

export function taskListCalls(api: FakeQueueApi): QueueApiCall[] {
  return api.calls.filter((c) => c.path === '/api/admin/tasks' && c.method === 'GET')
}

export function reviewCalls(api: FakeQueueApi): QueueApiCall[] {
  return api.calls.filter(
    (c) => c.path.startsWith('/api/accounts/') && c.path.endsWith('/review') && c.method === 'POST',
  )
}

export function signCalls(api: FakeQueueApi): QueueApiCall[] {
  return api.calls.filter(
    (c) => c.path.startsWith('/api/accounts/') && c.path.endsWith('/sign') && c.method === 'POST',
  )
}
