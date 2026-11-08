import type { Page, Route } from '@playwright/test'
import type { AccountSummary } from '../../src/lib/api/account'
import type { AuditStatusReport } from '../../src/lib/api/audit'
import type { DiagnosticsItem, SignStatusItem } from '../../src/lib/api/me'
import type {
  CapacityReport,
  ScheduleWindow,
  TimePref,
} from '../../src/lib/api/schedule'
import type { PrefStatsReport, SignStatsReport } from '../../src/lib/api/stats'
import { SESSION_CSRF, SESSION_EMAIL, DEFAULT_AUDIT_STATUS, type Role } from './fake-accounts-api'

/**
 * 看板的假 API。
 *
 * 一份路由处理 `/api/auth/me`、`/api/me/sign-status`、`/api/me/diagnostics`、
 * `/api/me/time-pref`、`/api/schedule/windows`、`/api/capacity`、
 * `/api/sign-stats`、`/api/time-prefs/stats`、`/api/accounts/summary`
 * 与 `/api/admin/audit-status`。
 * 响应体只用登记过的合成值：学校与任务带「合成」前缀，账号是 `acc-000N`。
 * 角色按契约 `x-roles` 判：sign-status 仅 user；windows、capacity、summary
 * 与 audit-status 仅 master/admin。
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
  signStats?: SignStatsReport
  prefStats?: PrefStatsReport
  signStatsError?: Failure
  prefStatsError?: Failure
  /** 待处理账号计数。缺省 {pending: 2, rejected: 1}，第 5 卡值 3。 */
  accountSummary?: AccountSummary
  accountSummaryError?: Failure
  timePref?: TimePref
  /** 整份容量报告覆盖：用来给单个用例换瓶颈归属，缺省按选中的窗口生成。 */
  capacity?: CapacityReport
  /** 审计链巡检结论。缺省完好 128 行；断链用例覆盖 ok/empty/broken_at。 */
  auditStatus?: AuditStatusReport
  auditStatusError?: Failure
}

const SIGN_STATUS_RUN_AT = '2026-10-28T06:41:00+08:00'

/** 巡检夹具与其它页面共用同一份，断链用例以展开覆盖单字段。 */
export { DEFAULT_AUDIT_STATUS }

/** 契约枚举的运行时副本，用来收窄窗口行上的 `string | null`。 */
const CAPACITY_SOURCES: readonly CapacityReport['source'][] = [
  'platform',
  'cache',
  'fallback',
  'override',
]

/**
 * 读面身份字段的合成值：展示名与服务端派生同形（账号+尾 4 位），
 * 手机号只出掩码。看板目前不渲染这三项，夹具只保证契约必填。
 */
export function accountIdentity(
  accountId: string,
): Pick<SignStatusItem, 'display_name' | 'phone_masked' | 'account_status'> {
  const suffix = accountId.slice(-4)
  return {
    display_name: `账号${suffix}`,
    phone_masked: `138****${suffix}`,
    account_status: 'active',
  }
}

/** 覆盖 success、pending、failed 与 null 四种状态。 */
export const DEFAULT_SIGN_STATUS: SignStatusItem[] = [
  { ...accountIdentity('acc-0001'), account_id: 'acc-0001', state: 'success', run_at: SIGN_STATUS_RUN_AT, stale: false },
  { ...accountIdentity('acc-0002'), account_id: 'acc-0002', state: 'pending', run_at: null, stale: false },
  { ...accountIdentity('acc-0003'), account_id: 'acc-0003', state: 'failed', run_at: '2026-10-28T06:52:00+08:00', stale: false },
  { ...accountIdentity('acc-0004'), account_id: 'acc-0004', state: null, run_at: null, stale: false },
]

/** 四条建议各一条，覆盖契约枚举全值。 */
export const DEFAULT_DIAGNOSTICS: DiagnosticsItem[] = [
  {
    ...accountIdentity('acc-0001'),
    account_id: 'acc-0001',
    state: 'success',
    run_at: SIGN_STATUS_RUN_AT,
    attempts: 1,
    result: '成功',
    next_retry_at: null,
    suggestion: 'wait',
  },
  {
    ...accountIdentity('acc-0002'),
    account_id: 'acc-0002',
    state: 'pending',
    run_at: null,
    attempts: 0,
    result: '',
    next_retry_at: null,
    suggestion: 'retry_now',
  },
  {
    ...accountIdentity('acc-0003'),
    account_id: 'acc-0003',
    state: 'failed',
    run_at: '2026-10-28T06:52:00+08:00',
    attempts: 2,
    result: '失败',
    next_retry_at: '2026-10-28T20:15:00+08:00',
    suggestion: 're_arm',
  },
  {
    ...accountIdentity('acc-0004'),
    account_id: 'acc-0004',
    state: null,
    run_at: null,
    attempts: 0,
    result: '',
    next_retry_at: null,
    suggestion: 'contact_admin',
  },
]

export const DEFAULT_TIME_PREF: TimePref = {
  offset_min: 15,
  allowed: true,
  window: { start: '06:00', end: '06:30' },
  estimated: '06:15',
  estimate_note: '（今日计划，非承诺）',
}

/** 待处理账号计数的缺省夹具：与容量/统计缺省同套合成口径。 */
export const DEFAULT_ACCOUNT_SUMMARY: AccountSummary = { pending: 2, rejected: 1 }

/** 30 天终态：固定到 DASHBOARD_DAY，视觉基线不随日历漂移。 */
export const DEFAULT_SIGN_STATS: SignStatsReport = {
  days: Array.from({ length: 30 }, (_, index) => {
    const date = new Date(Date.UTC(2026, 8, 29))
    date.setUTCDate(date.getUTCDate() + index)
    return {
      day: date.toISOString().slice(0, 10),
      pending: index === 29 ? 1 : 0,
      running: 0,
      success: 3,
      failed: index % 3 === 0 ? 1 : 0,
      skipped: index % 5 === 0 ? 1 : 0,
    }
  }),
  total: 10,
  by_status: { pending: 1, running: 0, success: 9, failed: 4, skipped: 3 },
  days_missing: [],
}

/** 合成窗口 08:30–09:00 的六个槽；人数固定。 */
export const DEFAULT_PREF_STATS: PrefStatsReport = {
  school_id: 'school-alpha',
  task_id: 'task-daily',
  day: DASHBOARD_DAY,
  window_start: `${DASHBOARD_DAY}T08:30:00+08:00`,
  window_end: `${DASHBOARD_DAY}T09:00:00+08:00`,
  slots: [2, 4, 1, 0, 0, 0].map((count, index) => ({
    offset_min: index * 5,
    label: `08:${String(30 + index * 5).padStart(2, '0')}`,
    count,
    cap: 5,
  })),
}

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
      ? {
          capacity: 40,
          demand: 12,
          shortfall: 0,
          binding: 'workers' as const,
          suggested_workers: 4,
        }
      : row.school_id === 'school-beta' && row.task_id === 'task-night'
        ? {
            capacity: 20,
            demand: 26,
            shortfall: 6,
            binding: 'workers' as const,
            suggested_workers: 4,
          }
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

/** 按选中任务生成槽：槽数与服务端同逻辑（整 5 分钟片，最多两小时）。 */
function prefStatsFor(day: string, schoolId: string, taskId: string): PrefStatsReport {
  const rows = dashboardWindows(day)
  const row = rows.find((item) => item.school_id === schoolId && item.task_id === taskId)
  if (!row || row.start === null || row.end === null) {
    return { ...DEFAULT_PREF_STATS, school_id: schoolId, task_id: taskId, day, slots: [] }
  }
  const [hour, minute] = row.start.slice(11, 16).split(':').map(Number)
  const spanMinutes = (Date.parse(row.end) - Date.parse(row.start)) / 60_000
  const slices = Math.max(0, Math.min(24, Math.floor(spanMinutes / 5)))
  const counts =
    taskId === 'task-night'
      ? [0, 1, 3, 2, 0, 0, 1, 0, 0, 0, 2, 0]
      : [2, 4, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]
  return {
    school_id: row.school_id,
    task_id: row.task_id,
    day,
    window_start: row.start,
    window_end: row.end,
    slots: Array.from({ length: slices }, (_, index) => {
      const total = hour * 60 + minute + index * 5
      return {
        offset_min: index * 5,
        label: `${String(Math.floor(total / 60) % 24).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`,
        count: counts[index] ?? 0,
        cap: 5,
      }
    }),
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
  const auditStatus: AuditStatusReport = { ...(options.auditStatus ?? DEFAULT_AUDIT_STATUS) }
  const failures: Record<
    | 'signStatus'
    | 'diagnostics'
    | 'windows'
    | 'capacity'
    | 'signStats'
    | 'prefStats'
    | 'accountSummary'
    | 'auditStatus',
    FailureEntry
  > = {
    signStatus: { spec: options.signStatusError, hits: 0 },
    diagnostics: { spec: options.diagnosticsError, hits: 0 },
    windows: { spec: options.windowsError, hits: 0 },
    capacity: { spec: options.capacityError, hits: 0 },
    signStats: { spec: options.signStatsError, hits: 0 },
    prefStats: { spec: options.prefStatsError, hits: 0 },
    accountSummary: { spec: options.accountSummaryError, hits: 0 },
    auditStatus: { spec: options.auditStatusError, hits: 0 },
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
      return json(route, options.capacity ?? capacityFor(day, row))
    }

    if (url.pathname === '/api/sign-stats' && request.method() === 'GET') {
      if (!(await deny(route, ['master', 'admin']))) return
      const failure = takeFailure('signStats')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      return json(route, options.signStats ?? DEFAULT_SIGN_STATS)
    }

    if (url.pathname === '/api/time-prefs/stats' && request.method() === 'GET') {
      if (!(await deny(route, ['master', 'admin']))) return
      const failure = takeFailure('prefStats')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      if (options.prefStats) return json(route, options.prefStats)
      const day = options.day ?? DASHBOARD_DAY
      const schoolId = url.searchParams.get('school_id') ?? ''
      const taskId = url.searchParams.get('task_id') ?? ''
      return json(route, prefStatsFor(day, schoolId, taskId))
    }

    // 待处理账号计数：静态段，先于 /api/accounts/:id 的兜底匹配。
    if (url.pathname === '/api/accounts/summary' && request.method() === 'GET') {
      if (!(await deny(route, ['master', 'admin']))) return
      const failure = takeFailure('accountSummary')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      return json(route, options.accountSummary ?? DEFAULT_ACCOUNT_SUMMARY)
    }

    if (url.pathname === '/api/admin/audit-status' && request.method() === 'GET') {
      if (!(await deny(route, ['master', 'admin']))) return
      const failure = takeFailure('auditStatus')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      return json(route, auditStatus)
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

export function auditStatusCalls(api: FakeDashboardApi): DashboardApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'GET' && call.path === '/api/admin/audit-status',
  )
}

export function signStatsCalls(api: FakeDashboardApi): DashboardApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/sign-stats')
}

export function prefStatsCalls(api: FakeDashboardApi): DashboardApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'GET' && call.path === '/api/time-prefs/stats',
  )
}

export function accountSummaryCalls(api: FakeDashboardApi): DashboardApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'GET' && call.path === '/api/accounts/summary',
  )
}

export function lastCapacityCall(api: FakeDashboardApi): DashboardApiCall | undefined {
  return capacityCalls(api).at(-1)
}
