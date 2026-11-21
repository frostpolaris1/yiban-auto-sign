import type { Page, Route } from '@playwright/test'
import type {
  AdminQuotaSettingsUpdate,
  AdminScheduleSettingsUpdate,
  AutoApproveStatus,
  QuotaReport,
  RegistrationStatus,
  ScheduleSettingsReport,
  SettingsReport,
} from '../../src/lib/api/adminSettings'
import type { Announcement } from '../../src/lib/api/announcement'
import type { SchedulePlan, ScheduleWindow, TimePref } from '../../src/lib/api/schedule'
import { EMPTY_ACCOUNT_SUMMARY, SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'

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

export const DEFAULT_TIME_PREF: TimePref = {
  offset_min: 15,
  allowed: true,
  window: { start: '06:00', end: '06:30' },
  estimated: '06:15',
  estimate_note: '（今日计划，非承诺）',
}

export type ScheduleApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

type Failure = {
  status: number
  code: string
  message: string
  details?: Record<string, unknown>
  retryAfterSec?: number
}

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
  registration?: RegistrationStatus
  autoApprove?: AutoApproveStatus
  quota?: QuotaReport
  scheduleSettings?: ScheduleSettingsReport
  announcement?: Announcement
  onPolicyWrite?: (method: string, path: string) => Failure | undefined
  onMailTest?: (body: { current_password?: string }) => Failure | undefined
  onWebhookTest?: () => Failure | undefined
}

export type FakeScheduleApi = {
  calls: ScheduleApiCall[]
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
  let registration: RegistrationStatus = {
    enabled: false,
    terms_version: 1,
    privacy_version: 1,
    ...(options.registration ?? {}),
  }
  let autoApprove: AutoApproveStatus = { enabled: true, ...(options.autoApprove ?? {}) }
  let quota: QuotaReport = {
    max_users: { value: 1, source: 'default' },
    max_accounts: { value: 1, source: 'default' },
    ...(options.quota ?? {}),
  }
  let scheduleSettings: ScheduleSettingsReport = {
    edge_front_sec: { value: 60, source: 'default' },
    edge_back_sec: { value: 60, source: 'default' },
    allow_time_pref: { value: false, source: 'default' },
    sunday_sign: { value: false, source: 'default' },
    saturday_sign: { value: false, source: 'default' },
    default_sign_start_min: { value: 390, source: 'default' },
    default_sign_end_min: { value: 470, source: 'default' },
    sign_dist: { value: 'front', source: 'default' },
    ...(options.scheduleSettings ?? {}),
  }
  let announcement: Announcement = {
    text: '示例公告：系统每晚自动更新次日签到计划，请在每日 06:00 前确认账号状态。',
    updated_at: '2026-11-18T10:00:00Z',
    ...(options.announcement ?? {}),
  }

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

  const fail = (
    route: Route,
    status: number,
    code: string,
    message: string,
    extra?: { details?: Record<string, unknown>; retryAfterSec?: number },
  ) => {
    const headers: Record<string, string> = {}
    if (extra?.retryAfterSec) {
      headers['Retry-After'] = String(extra.retryAfterSec)
    }
    return route.fulfill({
      status,
      headers,
      contentType: 'application/json',
      body: JSON.stringify({
        error: {
          code,
          message,
          details: extra?.details ?? { source: 'fake-schedule-api' },
          trace_id: 'trace-test',
        },
      }),
    })
  }

  const failFrom = (route: Route, failure: Failure) =>
    fail(route, failure.status, failure.code, failure.message, {
      details: failure.details,
      retryAfterSec: failure.retryAfterSec,
    })

  const requireMaster = async (route: Route): Promise<boolean> => {
    if (sessionRole === 'master') return true
    await fail(route, 403, 'forbidden', 'master role is required')
    return false
  }

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

    if (url.pathname === '/api/accounts/summary' && request.method() === 'GET') {
      // 壳在每页为 master/admin 读一次；时间片页不关心计数。
      return json(route, EMPTY_ACCOUNT_SUMMARY)
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

    if (url.pathname === '/api/schedule/windows/refresh' && request.method() === 'POST') {
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

    if (url.pathname === '/api/auth/registration') {
      if (request.method() === 'GET') {
        return json(route, registration)
      }
      if (request.method() === 'PUT') {
        if (!(await requireMaster(route))) return
        const injected = options.onPolicyWrite?.(request.method(), url.pathname)
        if (injected) return failFrom(route, injected)
        const patch = (body ?? {}) as { enabled: boolean }
        registration = { ...registration, enabled: Boolean(patch.enabled) }
        return json(route, registration)
      }
    }

    if (url.pathname === '/api/admin/auto-approve') {
      if (!(await requireManager(route))) return
      if (request.method() === 'GET') {
        return json(route, autoApprove)
      }
      if (request.method() === 'PUT') {
        if (!(await requireMaster(route))) return
        const injected = options.onPolicyWrite?.(request.method(), url.pathname)
        if (injected) return failFrom(route, injected)
        const patch = (body ?? {}) as { enabled: boolean }
        autoApprove = { enabled: Boolean(patch.enabled) }
        return json(route, autoApprove)
      }
    }

    if (url.pathname === '/api/admin/quota-settings') {
      if (!(await requireManager(route))) return
      if (request.method() === 'GET') {
        return json(route, quota)
      }
      if (request.method() === 'PUT') {
        if (!(await requireMaster(route))) return
        const injected = options.onPolicyWrite?.(request.method(), url.pathname)
        if (injected) return failFrom(route, injected)
        const patch = (body ?? {}) as AdminQuotaSettingsUpdate
        if (Object.keys(patch).length === 0) {
          return fail(route, 400, 'validation', 'empty quota patch')
        }
        if ('max_users' in patch) {
          quota = {
            ...quota,
            max_users:
              patch.max_users === null
                ? { value: 1, source: 'default' }
                : { value: patch.max_users!, source: 'db' },
          }
        }
        if ('max_accounts' in patch) {
          quota = {
            ...quota,
            max_accounts:
              patch.max_accounts === null
                ? { value: 1, source: 'default' }
                : { value: patch.max_accounts!, source: 'db' },
          }
        }
        return json(route, quota)
      }
    }

    if (url.pathname === '/api/admin/schedule-settings') {
      if (!(await requireManager(route))) return
      if (request.method() === 'GET') {
        return json(route, scheduleSettings)
      }
      if (request.method() === 'PUT') {
        if (!(await requireMaster(route))) return
        const injected = options.onPolicyWrite?.(request.method(), url.pathname)
        if (injected) return failFrom(route, injected)
        const patch = (body ?? {}) as AdminScheduleSettingsUpdate
        if (Object.keys(patch).length === 0) {
          return fail(route, 400, 'validation', 'empty schedule patch')
        }
        if ('edge_front_sec' in patch) {
          scheduleSettings.edge_front_sec =
            patch.edge_front_sec === null
              ? { value: 60, source: 'default' }
              : { value: patch.edge_front_sec!, source: 'db' }
        }
        if ('edge_back_sec' in patch) {
          scheduleSettings.edge_back_sec =
            patch.edge_back_sec === null
              ? { value: 60, source: 'default' }
              : { value: patch.edge_back_sec!, source: 'db' }
        }
        if ('allow_time_pref' in patch) {
          scheduleSettings.allow_time_pref =
            patch.allow_time_pref === null
              ? { value: false, source: 'default' }
              : { value: patch.allow_time_pref!, source: 'db' }
        }
        if ('sunday_sign' in patch) {
          scheduleSettings.sunday_sign =
            patch.sunday_sign === null
              ? { value: false, source: 'default' }
              : { value: patch.sunday_sign!, source: 'db' }
        }
        if ('saturday_sign' in patch) {
          scheduleSettings.saturday_sign =
            patch.saturday_sign === null
              ? { value: false, source: 'default' }
              : { value: patch.saturday_sign!, source: 'db' }
        }
        if ('default_sign_start_min' in patch || 'default_sign_end_min' in patch) {
          const nextStart = patch.default_sign_start_min
          const nextEnd = patch.default_sign_end_min
          if (nextStart === null || nextEnd === null) {
            scheduleSettings.default_sign_start_min = { value: 390, source: 'default' }
            scheduleSettings.default_sign_end_min = { value: 470, source: 'default' }
          } else {
            const startVal = nextStart ?? scheduleSettings.default_sign_start_min.value
            const endVal = nextEnd ?? scheduleSettings.default_sign_end_min.value
            if (startVal >= endVal) {
              return fail(route, 400, 'validation', 'default_sign_start_min must be before end')
            }
            scheduleSettings.default_sign_start_min = { value: startVal, source: 'db' }
            scheduleSettings.default_sign_end_min = { value: endVal, source: 'db' }
          }
        }
        if ('sign_dist' in patch) {
          scheduleSettings.sign_dist =
            patch.sign_dist === null
              ? { value: 'front', source: 'default' }
              : { value: patch.sign_dist!, source: 'db' }
        }
        const report: SettingsReport = {
          groups: [
            {
              name: '调度',
              items: [
                {
                  key: 'YIBAN_WINDOW_EDGE_FRONT_SEC',
                  group: '调度',
                  source: scheduleSettings.edge_front_sec.source,
                  value: scheduleSettings.edge_front_sec.value,
                  sensitive: null,
                },
                {
                  key: 'YIBAN_WINDOW_EDGE_BACK_SEC',
                  group: '调度',
                  source: scheduleSettings.edge_back_sec.source,
                  value: scheduleSettings.edge_back_sec.value,
                  sensitive: null,
                },
                {
                  key: 'YIBAN_ALLOW_TIME_PREF',
                  group: '调度',
                  source: scheduleSettings.allow_time_pref.source,
                  value: scheduleSettings.allow_time_pref.value,
                  sensitive: null,
                },
              ],
            },
          ],
        }
        return json(route, report)
      }
    }

    if (url.pathname === '/api/announcement' && request.method() === 'GET') {
      return json(route, announcement)
    }

    if (url.pathname === '/api/admin/announcement' && request.method() === 'PUT') {
      if (!(await requireMaster(route))) return
      const injected = options.onPolicyWrite?.(request.method(), url.pathname)
      if (injected) return failFrom(route, injected)
      const payload = (body ?? {}) as { text?: string }
      const text = (payload.text ?? '').trim()
      if (/[\r\n\u2028\u2029]/.test(text) || Array.from(text).length > 200) {
        return fail(route, 400, 'validation', 'invalid announcement body')
      }
      announcement =
        text === ''
          ? { text: '', updated_at: null }
          : { text, updated_at: '2026-11-19T01:00:00Z' }
      return json(route, announcement)
    }

    if (url.pathname === '/api/admin/mail/test' && request.method() === 'POST') {
      if (!(await requireMaster(route))) return
      const payload = (body ?? {}) as { current_password?: string }
      const injected = options.onMailTest?.(payload)
      if (injected) return failFrom(route, injected)
      if (!payload.current_password) {
        return fail(route, 400, 'validation', 'current_password is required')
      }
      return json(route, { message: 'test email accepted by the SMTP server' })
    }

    if (url.pathname === '/api/admin/webhook/test' && request.method() === 'POST') {
      if (!(await requireMaster(route))) return
      const injected = options.onWebhookTest?.()
      if (injected) return failFrom(route, injected)
      return json(route, { message: 'test webhook accepted by the outlet' })
    }

    return fail(route, 404, 'not_found', '未找到资源')
  })

  return { calls }
}

export function scheduleCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter((call) => call.path.startsWith('/api/schedule/'))
}

export function windowsCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter(
    (call) =>
      (call.method === 'GET' && call.path === '/api/schedule/windows') ||
      (call.method === 'POST' && call.path === '/api/schedule/windows/refresh'),
  )
}

export function windowsRefreshCalls(api: FakeScheduleApi): ScheduleApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === '/api/schedule/windows/refresh',
  )
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
