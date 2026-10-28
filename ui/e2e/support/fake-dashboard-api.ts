import type { Page, Route } from '@playwright/test'
import type { DiagnosticsItem, SignStatusItem } from '../../src/lib/api/me'
import type {
  CapacityReport,
  ScheduleWindow,
  TimePref,
} from '../../src/lib/api/schedule'
import { SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'

/**
 * 看板的假 API。
 *
 * 一份路由处理 `/api/auth/me`、`/api/me/sign-status`、`/api/me/diagnostics`、
 * `/api/me/time-pref`、`/api/schedule/windows` 与 `/api/capacity`。
 * 响应体只用登记过的合成值：学校与任务带「合成」前缀，账号是 `acc-000N`。
 * 角色按契约 `x-roles` 判：sign-status 仅 user；windows 与 capacity 仅 master/admin。
 * 调用记录供断言「零请求」「参数正确」与「轮询次数」。
 * 错误注入默认每次都失败；`once: true` 时只有第一次失败，用来验「重试」。
 */

export type DashboardApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

export type Failure = {
  status: number
  code: string
  message: string
  /** 只让第一次请求失败；之后成功。 */
  once?: boolean
}

/** 固定业务日：视觉基线不得随日历漂移（与时间片页同口径）。 */
export const DASHBOARD_DAY = '2026-10-28'

export type MockDashboardOptions = {
  role: Role | null
  /** 回显给界面的业务日；缺省 DASHBOARD_DAY。 */
  day?: string
  signStatusItems?: SignStatusItem[]
  diagnosticsItems?: DiagnosticsItem[]
  /** 缺省回显 DASHBOARD_DAY 并据此生成四行（不随请求日漂移）。 */
  windows?: ScheduleWindow[]
  signStatusError?: Failure
  diagnosticsError?: Failure
  windowsError?: Failure
  capacityError?: Failure
  timePref?: TimePref
}

const SIGN_STATUS_RUN_AT = '2026-10-28T06:41:00+08:00'

/** 契约枚举的运行时副本，用来收窄窗口行上的 `string | null`。 */
const CAPACITY_SOURCES: readonly CapacityReport['source'][] = [
  'platform',
  'cache',
  'fallback',
  'override',
]

/** 覆盖 success、pending、failed 与 null 四种状态。 */
export const DEFAULT_SIGN_STATUS: SignStatusItem[] = [
  { account_id: 'acc-0001', state: 'success', run_at: SIGN_STATUS_RUN_AT, stale: false },
  { account_id: 'acc-0002', state: 'pending', run_at: null, stale: false },
  { account_id: 'acc-0003', state: 'failed', run_at: '2026-10-28T06:52:00+08:00', stale: false },
  { account_id: 'acc-0004', state: null, run_at: null, stale: false },
]

/** 四条建议各一条，覆盖契约枚举全值。 */
export const DEFAULT_DIAGNOSTICS: DiagnosticsItem[] = [
  {
    account_id: 'acc-0001',
    state: 'success',
    run_at: SIGN_STATUS_RUN_AT,
    attempts: 1,
    result: '成功',
    next_retry_at: null,
    suggestion: 'wait',
  },
  {
    account_id: 'acc-0002',
    state: 'pending',
    run_at: null,
    attempts: 0,
    result: '',
    next_retry_at: null,
    suggestion: 'retry_now',
  },
  {
    account_id: 'acc-0003',
    state: 'failed',
    run_at: '2026-10-28T06:52:00+08:00',
    attempts: 2,
    result: '失败',
    next_retry_at: '2026-10-28T20:15:00+08:00',
    suggestion: 're_arm',
  },
  {
    account_id: 'acc-0004',
    state: null,
    run_at: null,
    attempts: 0,
    result: '',
    next_retry_at: null,
    suggestion: 'contact_admin',
  },
]

export const DEFAULT_TIME_PREF: TimePref = { offset_min: 15, allowed: true }

/** 四行窗口：两行可用、一行停用、一行平台未报窗口。 */
export function dashboardWindows(day: string): ScheduleWindow[] {
  return [
    {
      school_id: 'school-alpha',
      school_name: '合成学校甲',
      task_id: 'task-daily',
      task_name: '每日签到',
      enabled: true,
      day,
      start: `${day}T08:30:00+08:00`,
      end: `${day}T09:00:00+08:00`,
      source: 'platform',
      version: 2,
    },
    {
      school_id: 'school-beta',
      school_name: '合成学校乙',
      task_id: 'task-night',
      task_name: '夜间签到',
      enabled: true,
      day,
      start: `${day}T20:00:00+08:00`,
      end: `${day}T21:00:00+08:00`,
      source: 'override',
      version: 3,
    },
    {
      school_id: 'school-gamma',
      school_name: '合成学校丙',
      task_id: 'task-weekly',
      task_name: '周报签到',
      enabled: false,
      day,
      start: `${day}T10:00:00+08:00`,
      end: `${day}T11:00:00+08:00`,
      source: 'fallback',
      version: 0,
    },
    {
      school_id: 'school-beta',
      school_name: '合成学校乙',
      task_id: 'task-legacy',
      task_name: '旧版签到',
      enabled: true,
      day,
      start: null,
      end: null,
      source: null,
      version: 0,
    },
  ]
}

function capacityFor(
  day: string,
  row: ScheduleWindow,
): CapacityReport {
  const numbers =
    row.school_id === 'school-alpha' && row.task_id === 'task-daily'
      ? { capacity: 40, demand: 12, shortfall: 0 }
      : row.school_id === 'school-beta' && row.task_id === 'task-night'
        ? { capacity: 20, demand: 26, shortfall: 6 }
        : { capacity: 0, demand: 0, shortfall: 0 }
  return {
    school_id: row.school_id,
    task_id: row.task_id,
    day,
    source: CAPACITY_SOURCES.find((item) => item === row.source) ?? 'fallback',
    window_start: row.start ?? `${day}T00:00:00+08:00`,
    window_end: row.end ?? `${day}T00:00:00+08:00`,
    ...numbers,
  }
}

export type FakeDashboardApi = {
  calls: DashboardApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-dashboard-api' }, trace_id: 'trace-test' },
  })
}

type FailureEntry = { spec: Failure | undefined; hits: number }

export async function mockDashboardApi(
  page: Page,
  options: MockDashboardOptions,
): Promise<FakeDashboardApi> {
  const calls: DashboardApiCall[] = []
  const sessionRole = options.role
  const signStatusItems = (options.signStatusItems ?? DEFAULT_SIGN_STATUS).map((item) => ({
    ...item,
  }))
  const diagnosticsItems = (options.diagnosticsItems ?? DEFAULT_DIAGNOSTICS).map((item) => ({
    ...item,
  }))
  const timePref: TimePref = { ...(options.timePref ?? DEFAULT_TIME_PREF) }
  const failures: Record<'signStatus' | 'diagnostics' | 'windows' | 'capacity', FailureEntry> = {
    signStatus: { spec: options.signStatusError, hits: 0 },
    diagnostics: { spec: options.diagnosticsError, hits: 0 },
    windows: { spec: options.windowsError, hits: 0 },
    capacity: { spec: options.capacityError, hits: 0 },
  }

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })

  const takeFailure = (key: keyof typeof failures): Failure | null => {
    const entry = failures[key]
    if (!entry.spec) return null
    entry.hits += 1
    if (entry.spec.once && entry.hits > 1) return null
    return entry.spec
  }

  const deny = async (route: Route, roles: Role[]): Promise<boolean> => {
    if (sessionRole !== null && roles.includes(sessionRole)) return true
    await fail(route, 403, 'forbidden', '无权访问')
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

    if (url.pathname === '/api/me/sign-status' && request.method() === 'GET') {
      if (!(await deny(route, ['user']))) return
      const failure = takeFailure('signStatus')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      return json(route, { updated_at: SIGN_STATUS_RUN_AT, items: signStatusItems })
    }

    if (url.pathname === '/api/me/diagnostics' && request.method() === 'GET') {
      if (!(await deny(route, ['user', 'admin']))) return
      const failure = takeFailure('diagnostics')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      return json(route, { updated_at: SIGN_STATUS_RUN_AT, items: diagnosticsItems })
    }

    if (url.pathname === '/api/me/time-pref') {
      if (!(await deny(route, ['user']))) return
      return json(route, timePref)
    }

    if (url.pathname === '/api/schedule/windows' && request.method() === 'GET') {
      if (!(await deny(route, ['master', 'admin']))) return
      const failure = takeFailure('windows')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      const day = options.day ?? DASHBOARD_DAY
      return json(route, { day, items: options.windows ?? dashboardWindows(day) })
    }

    if (url.pathname === '/api/capacity' && request.method() === 'GET') {
      if (!(await deny(route, ['master', 'admin']))) return
      const failure = takeFailure('capacity')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      const day = options.day ?? DASHBOARD_DAY
      const schoolId = url.searchParams.get('school_id')
      const taskId = url.searchParams.get('task_id')
      if (!schoolId || !taskId) return fail(route, 400, 'invalid_argument', '缺少定位参数')
      const rows = options.windows ?? dashboardWindows(day)
      const row = rows.find((item) => item.school_id === schoolId && item.task_id === taskId)
      if (!row || !row.start || !row.end) {
        return fail(route, 404, 'not_found', '任务窗口不存在')
      }
      return json(route, capacityFor(day, row))
    }

    return fail(route, 404, 'not_found', '未找到资源')
  })

  return { calls }
}

export function signStatusCalls(api: FakeDashboardApi): DashboardApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/me/sign-status')
}

export function diagnosticsCalls(api: FakeDashboardApi): DashboardApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/me/diagnostics')
}

export function windowsCalls(api: FakeDashboardApi): DashboardApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/schedule/windows')
}

export function capacityCalls(api: FakeDashboardApi): DashboardApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/capacity')
}

export function lastCapacityCall(api: FakeDashboardApi): DashboardApiCall | undefined {
  return capacityCalls(api).at(-1)
}
