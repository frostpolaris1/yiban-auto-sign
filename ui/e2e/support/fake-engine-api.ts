import type { Page, Route } from '@playwright/test'
import type { EngineSnapshot } from '../../src/lib/api/engine'
import { SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'

/**
 * 执行监控页的假 API（UX-5）。
 *
 * 一份路由处理 `/api/auth/me` 与 `GET /api/admin/engine`。
 * 响应体只用登记过的合成值：掩码手机号取 138 保留段，学校名明示合成口径。
 * 调用记录供断言「无权限时发没发请求」与断线轮询。
 * 时刻相对当下取值：租约剩余与倒计时才有可断言的活值。
 * SSE 不在这里伪造：用例经 `installFakeEventSource` 注入假体。
 */

/** 相对当下的 ISO 时刻。 */
function at(offsetMs: number): string {
  return new Date(Date.now() + offsetMs).toISOString()
}

const MINUTE = 60_000

/** 登记过的合成快照：两个在飞、两个待重试（同账号两条）、计数非零。 */
export const ENGINE_SNAPSHOT: EngineSnapshot = {
  inflight: [
    {
      account_id: 'account-1',
      account_phone_masked: '138****8001',
      school_id: 'school-a',
      school_name: '合成学校甲',
      claimed_at: at(-40_000),
      lease_expires_at: at(80_000),
    },
    {
      account_id: 'account-4',
      account_phone_masked: '138****8004',
      school_id: 'school-b',
      school_name: '合成学校乙',
      claimed_at: at(-15_000),
      lease_expires_at: at(105_000),
    },
  ],
  due_now: 3,
  today_success: 12,
  today_failed: 1,
  retrying: [
    {
      task_id: 'task-r1',
      account_id: 'account-3',
      account_phone_masked: '138****8003',
      school_id: 'school-a',
      school_name: '合成学校甲',
      task_name: '早签到',
      attempts: 2,
      run_at: at(5 * MINUTE),
      result: 'transient',
    },
    {
      task_id: 'task-r2',
      account_id: 'account-3',
      account_phone_masked: '138****8003',
      school_id: 'school-a',
      school_name: '合成学校甲',
      task_name: '晚自习签到',
      attempts: 1,
      run_at: at(20 * MINUTE),
      result: 'rejected',
    },
  ],
  retrying_truncated: false,
  last_claim_at: at(-40_000),
  last_tick_at: at(-2_000),
  worker_started_at: at(-6 * 60 * MINUTE),
}

/**
 * 视觉基线的页钟时刻：固定值，不随真实墙钟。
 * `/engine` 的视觉用例在 goto 前 `page.clock.setFixedTime(new Date(VISUAL_NOW))`，
 * 页内 `Date.now()` 与本夹具同源——租约剩余恒为「1 时 30 分」，
 * 相对时刻恒落在 formatClock 分支，基线跨日、跨分钟都不漂。
 */
export const VISUAL_NOW = Date.parse('2030-01-01T09:00:00+08:00')

/** 以 VISUAL_NOW 为基准的固定 ISO 时刻。 */
function visualAt(offsetMs: number): string {
  return new Date(VISUAL_NOW + offsetMs).toISOString()
}

/**
 * 视觉基线的合成快照：全部时刻派生自 VISUAL_NOW，零 Date.now()。
 * 值仍是合成标识：掩码号取 138 保留段，学校名明示合成口径。
 */
export const ENGINE_VISUAL_SNAPSHOT: EngineSnapshot = {
  ...ENGINE_SNAPSHOT,
  inflight: [
    {
      account_id: 'account-1',
      account_phone_masked: '138****8001',
      school_id: 'school-a',
      school_name: '合成学校甲',
      claimed_at: visualAt(-30 * MINUTE),
      lease_expires_at: visualAt(90 * MINUTE),
    },
    {
      account_id: 'account-4',
      account_phone_masked: '138****8004',
      school_id: 'school-b',
      school_name: '合成学校乙',
      claimed_at: visualAt(-25 * MINUTE),
      lease_expires_at: visualAt(95 * MINUTE),
    },
  ],
  retrying: [
    {
      task_id: 'task-r1',
      account_id: 'account-3',
      account_phone_masked: '138****8003',
      school_id: 'school-a',
      school_name: '合成学校甲',
      task_name: '早签到',
      attempts: 2,
      run_at: visualAt(60 * MINUTE),
      result: 'transient',
    },
  ],
  last_claim_at: visualAt(-2 * 60 * MINUTE),
  last_tick_at: visualAt(-2 * 60 * MINUTE),
  worker_started_at: visualAt(-6 * 60 * MINUTE),
}

export type FakeEngineApi = {
  calls: { method: string; path: string }[]
}

export type MockEngineOptions = {
  role: Role | null
  /** 覆盖快照：空在飞等场景由用例给。 */
  snapshot?: EngineSnapshot
  snapshotError?: { status: number; code: string; message: string }
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-engine-api' }, trace_id: 'trace-test' },
  })
}

export async function mockEngineApi(page: Page, options: MockEngineOptions): Promise<FakeEngineApi> {
  const calls: FakeEngineApi['calls'] = []
  const sessionRole = options.role

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    calls.push({ method: request.method(), path: url.pathname })

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

    if (url.pathname === '/api/admin/engine' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可查看执行监控')
      }
      if (options.snapshotError) {
        return fail(
          route,
          options.snapshotError.status,
          options.snapshotError.code,
          options.snapshotError.message,
        )
      }
      return json(route, options.snapshot ?? ENGINE_SNAPSHOT)
    }

    return fail(route, 404, 'not_found', `fake-engine-api 未登记 ${url.pathname}`)
  })

  return { calls }
}

/** 执行监控快照的调用记录。 */
export function engineCalls(api: FakeEngineApi) {
  return api.calls.filter((call) => call.path === '/api/admin/engine' && call.method === 'GET')
}
