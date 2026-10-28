import type { Page, Route } from '@playwright/test'
import type { SchedulePlan, ScheduleWindow, TimePref } from '../../src/lib/api/schedule'
import { SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'

/**
 * 时间片页与时间偏好卡的假 API。
 *
 * 一份路由处理 `/api/auth/me`、`/api/schedule/**` 与 `/api/me/time-pref`。
 * 响应体只用登记过的合成值：学校与任务都带「合成」前缀，账号 ID 是 `acc-000N`。
 * 调用记录供断言「请求带哪个参数」与「确认前发没发请求」。
 *
 * 写操作会改时间偏好状态，故每个测试各持一份副本，不污染共享夹具。
 */

export const SCHEDULE_DAY = '2026-10-28'

function row(index: number, runAt: string): { account_id: string; run_at: string } {
  return { account_id: `acc-${String(index).padStart(4, '0')}`, run_at: runAt }
}

/** 三行窗口：启用且有窗口、停用且有窗口、启用但平台未报窗口。 */
export const WINDOWS: ScheduleWindow[] = [
  {
    school_id: 'school-alpha',
    school_name: '合成学校甲',
    task_id: 'task-daily',
    task_name: '每日签到',
    enabled: true,
    day: SCHEDULE_DAY,
    start: '2026-10-28T00:10:00Z',
    end: '2026-10-28T01:30:00Z',
    source: 'platform',
    version: 2,
  },
  {
    school_id: 'school-gamma',
    school_name: '合成学校丙',
    task_id: 'task-weekly',
    task_name: '周报签到',
    enabled: false,
    day: SCHEDULE_DAY,
    start: '2026-10-28T02:00:00Z',
    end: '2026-10-28T03:00:00Z',
    source: 'fallback',
    version: 0,
  },
  {
    school_id: 'school-beta',
    school_name: '合成学校乙',
    task_id: 'task-night',
    task_name: '夜间签到',
    enabled: true,
    day: SCHEDULE_DAY,
    start: null,
    end: null,
    source: null,
    version: 0,
  },
]

/** 预览：启用任务的计划。直方图故意缺片，钉住按数字排序。 */
export const PLAN_ALPHA: SchedulePlan = {
  day: SCHEDULE_DAY,
  enabled: true,
  source: 'platform',
  window_start: '2026-10-28T00:10:00Z',
  window_end: '2026-10-28T01:30:00Z',
  version: 0,
  capacity: 40,
  demand: 12,
  shortfall: 0,
  histogram: { '0': 3, '1': 1, '3': 4, '4': 2, '6': 1, '7': 1 },
  rows: [
    row(1, '2026-10-28T00:10:00Z'),
    row(2, '2026-10-28T00:10:00Z'),
    row(3, '2026-10-28T00:10:00Z'),
    row(4, '2026-10-28T00:15:00Z'),
    row(5, '2026-10-28T00:25:00Z'),
    row(6, '2026-10-28T00:25:00Z'),
    row(7, '2026-10-28T00:25:00Z'),
    row(8, '2026-10-28T00:25:00Z'),
    row(9, '2026-10-28T00:30:00Z'),
    row(10, '2026-10-28T00:30:00Z'),
    row(11, '2026-10-28T00:40:00Z'),
    row(12, '2026-10-28T00:45:00Z'),
  ],
}

/** 预览：停用任务仍可预览，窗口照常给出。 */
export const PLAN_GAMMA: SchedulePlan = {
  day: SCHEDULE_DAY,
  enabled: false,
  source: 'fallback',
  window_start: '2026-10-28T02:00:00Z',
  window_end: '2026-10-28T03:00:00Z',
  version: 0,
  capacity: 12,
  demand: 4,
  shortfall: 0,
  histogram: { '0': 4 },
  rows: [
    row(101, '2026-10-28T02:00:00Z'),
    row(102, '2026-10-28T02:00:00Z'),
    row(103, '2026-10-28T02:05:00Z'),
    row(104, '2026-10-28T02:05:00Z'),
  ],
}

/** 覆盖成功后的重建计划：来源改 override，版本非零。 */
export const PLAN_REBUILT: SchedulePlan = {
  day: SCHEDULE_DAY,
  enabled: true,
  source: 'override',
  window_start: '2026-10-28T00:20:00Z',
  window_end: '2026-10-28T01:20:00Z',
  version: 3,
  capacity: 30,
  demand: 12,
  shortfall: 0,
  histogram: { '0': 2, '1': 4, '2': 6 },
  rows: PLAN_ALPHA.rows,
}

export const DEFAULT_TIME_PREF: TimePref = { offset_min: 15, allowed: true }

export type ScheduleApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

type Failure = { status: number; code: string; message: string }

export type MockScheduleOptions = {
  role: Role | null
  windows?: ScheduleWindow[]
  /** 键是 `${school_id}:${task_id}`。 */
  plans?: Record<string, SchedulePlan>
  windowsError?: Failure
  previewError?: Failure
  planError?: Failure
  overrideError?: Failure
  /** POST plan 与 POST override 的成功响应。缺省用登记的计划。 */
  planResult?: SchedulePlan
  overrideResult?: SchedulePlan
  timePref?: TimePref
  timePrefGetError?: Failure
  timePrefPutError?: Failure
}

export type FakeScheduleApi = {
  calls: ScheduleApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-schedule-api' }, trace_id: 'trace-test' },
  })
}

export async function mockScheduleApi(
  page: Page,
  options: MockScheduleOptions,
): Promise<FakeScheduleApi> {
  const calls: ScheduleApiCall[] = []
  const sessionRole = options.role
  const windows = (options.windows ?? WINDOWS).map((item) => ({ ...item }))
  const plans: Record<string, SchedulePlan> = options.plans ?? {
    'school-alpha:task-daily': PLAN_ALPHA,
    'school-gamma:task-weekly': PLAN_GAMMA,
  }
  let timePref: TimePref = { ...(options.timePref ?? DEFAULT_TIME_PREF) }

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })

  const requireManager = async (route: Route): Promise<boolean> => {
    if (sessionRole === 'master' || sessionRole === 'admin') return true
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

    if (url.pathname === '/api/schedule/windows' && request.method() === 'GET') {
      if (!(await requireManager(route))) return
      if (options.windowsError) {
        const error = options.windowsError
        return fail(route, error.status, error.code, error.message)
      }
      return json(route, {
        day: url.searchParams.get('day') ?? SCHEDULE_DAY,
        items: windows,
      })
    }

    if (url.pathname === '/api/schedule/preview' && request.method() === 'GET') {
      if (!(await requireManager(route))) return
      if (options.previewError) {
        const error = options.previewError
        return fail(route, error.status, error.code, error.message)
      }
      const key = `${url.searchParams.get('school_id')}:${url.searchParams.get('task_id')}`
      const plan = plans[key]
      if (!plan) return fail(route, 404, 'not_found', '任务不存在')
      return json(route, plan)
    }

    if (url.pathname === '/api/schedule/plan' && request.method() === 'POST') {
      if (!(await requireManager(route))) return
      if (options.planError) {
        const error = options.planError
        return fail(route, error.status, error.code, error.message)
      }
      return json(route, options.planResult ?? PLAN_ALPHA)
    }

    if (url.pathname === '/api/schedule/override' && request.method() === 'POST') {
      if (!(await requireManager(route))) return
      if (options.overrideError) {
        const error = options.overrideError
        return fail(route, error.status, error.code, error.message)
      }
      return json(route, options.overrideResult ?? PLAN_REBUILT)
    }

    if (url.pathname === '/api/me/time-pref') {
      if (sessionRole !== 'user') return fail(route, 403, 'forbidden', '此接口仅 user 可用')
      if (request.method() === 'GET') {
        if (options.timePrefGetError) {
          const error = options.timePrefGetError
          return fail(route, error.status, error.code, error.message)
        }
        return json(route, timePref)
      }
      if (request.method() === 'PUT') {
        if (options.timePrefPutError) {
          const error = options.timePrefPutError
          return fail(route, error.status, error.code, error.message)
        }
        const update = (body ?? {}) as { offset_min?: number | null }
        timePref = { ...timePref, offset_min: update.offset_min ?? null }
        return json(route, timePref)
      }
    }

    return fail(route, 404, 'not_found', '未找到资源')
  })

  return { calls }
}

export function scheduleCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter((call) => call.path.startsWith('/api/schedule/'))
}

export function windowsCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/schedule/windows')
}

export function lastWindowsCall(api: FakeScheduleApi): ScheduleApiCall | undefined {
  return windowsCalls(api).at(-1)
}

export function previewCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/schedule/preview')
}

export function planCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter((call) => call.method === 'POST' && call.path === '/api/schedule/plan')
}

export function overrideCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter((call) => call.method === 'POST' && call.path === '/api/schedule/override')
}

export function timePrefGetCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/api/me/time-pref')
}

export function timePrefPutCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter((call) => call.method === 'PUT' && call.path === '/api/me/time-pref')
}
