import type { Page, Route } from '@playwright/test'
import type { EngineSnapshot } from '../../src/lib/api/engine'
import type { AdminTaskRow } from '../../src/lib/api/task'
import { EMPTY_ACCOUNT_SUMMARY, SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'
import { ENGINE_SNAPSHOT, ENGINE_VISUAL_SNAPSHOT, VISUAL_NOW } from './fake-engine-api'
import {
  fakeSourceCloseCalls,
  fakeSourceClosed,
  fakeSourceCount,
  fakeSourceEmit,
  fakeSourceEnd,
  fakeSourceFail,
  fakeSourceHeartbeat,
  fakeSourceOpen,
  fakeSourceResync,
  fakeSourceSnapshot,
  installFakeEventSource,
  RUN_DIAGNOSTICS,
  RUN_TASKS,
  type ApiCall,
  type FakeEventFrame,
} from './fake-run-api'
import { handleDefaultNotifyRoute } from './fake-notify-api'
import { SCHOOLS } from './fake-schools-api'

/**
 * 签到台账（`/logs`）的组合假 API。
 *
 * 合并原 `fake-run-api` 与 `fake-engine-api` 的全部路由，使事件页签与巡检页签
 * 在同一次会话中切换时共用同一套请求计数与错误注入。
 */

export {
  ENGINE_SNAPSHOT,
  ENGINE_VISUAL_SNAPSHOT,
  VISUAL_NOW,
  RUN_DIAGNOSTICS,
  RUN_TASKS,
  fakeSourceCloseCalls,
  fakeSourceClosed,
  fakeSourceCount,
  fakeSourceEmit,
  fakeSourceEnd,
  fakeSourceFail,
  fakeSourceHeartbeat,
  fakeSourceOpen,
  fakeSourceResync,
  fakeSourceSnapshot,
  installFakeEventSource,
  type ApiCall,
  type FakeEventFrame,
}

export type MockLogsOptions = {
  role: Role | null
  /** 任务列表行。给了 `taskPages` 时忽略本项。 */
  tasks?: AdminTaskRow[]
  /** 键是本次请求的 cursor；首屏键是空串。 */
  taskPages?: Record<string, AdminTaskRow[]>
  taskNextCursors?: Record<string, string>
  listError?: { status: number; code: string; message: string }
  exportBody?: string
  exportError?: { status: number; code: string; message: string }
  diagnosticsError?: { status: number; code: string; message: string }
  /** 巡检快照覆盖：空在飞等场景由用例给。 */
  snapshot?: EngineSnapshot
  snapshotError?: { status: number; code: string; message: string }
}

export type FakeLogsApi = {
  calls: ApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-logs-api' }, trace_id: 'trace-test' },
  })
}

export async function mockLogsApi(page: Page, options: MockLogsOptions): Promise<FakeLogsApi> {
  const calls: ApiCall[] = []
  const sessionRole = options.role
  let engineSnapshotState = { ...(options.snapshot ?? ENGINE_SNAPSHOT) }
  const tasks = options.tasks ?? RUN_TASKS

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    calls.push({
      method: request.method(),
      path: url.pathname,
      search: url.search,
      params: Object.fromEntries(url.searchParams.entries()),
      body: request.postData() ?? null,
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
      return json(route, EMPTY_ACCOUNT_SUMMARY)
    }

    if (await handleDefaultNotifyRoute(route, request, url, sessionRole)) return

    if (url.pathname === '/api/schools' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可读学校列表')
      }
      return json(route, { items: SCHOOLS, next_cursor: '' })
    }

    if (url.pathname === '/api/admin/tasks' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可查看签到台账')
      }
      if (options.listError) {
        return fail(route, options.listError.status, options.listError.code, options.listError.message)
      }
      const cursor = url.searchParams.get('cursor') ?? ''
      const source = options.taskPages ? (options.taskPages[cursor] ?? []) : tasks
      const next = options.taskNextCursors?.[cursor] ?? ''
      return json(route, { items: source, next_cursor: next })
    }

    if (url.pathname === '/api/admin/tasks/export' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可导出')
      }
      if (options.exportError) {
        return fail(
          route,
          options.exportError.status,
          options.exportError.code,
          options.exportError.message,
        )
      }
      return route.fulfill({
        status: 200,
        contentType: 'text/csv; charset=utf-8',
        headers: { 'Content-Disposition': 'attachment; filename="tasks-2026-10-31.csv"' },
        body: options.exportBody ?? 'id,account_id\nx,y\n',
      })
    }

    if (url.pathname.startsWith('/api/accounts/') && url.pathname.endsWith('/diagnostics')) {
      if (options.diagnosticsError) {
        return fail(
          route,
          options.diagnosticsError.status,
          options.diagnosticsError.code,
          options.diagnosticsError.message,
        )
      }
      return json(route, RUN_DIAGNOSTICS)
    }

    if (url.pathname === '/api/admin/engine' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可查看运行巡检')
      }
      if (options.snapshotError) {
        return fail(
          route,
          options.snapshotError.status,
          options.snapshotError.code,
          options.snapshotError.message,
        )
      }
      return json(route, engineSnapshotState)
    }

    if (url.pathname === '/api/admin/engine/pause' && request.method() === 'PUT') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可操作暂停开关')
      }
      let update: { paused?: boolean; current_password?: string } = {}
      try {
        update = JSON.parse(request.postData() ?? '{}')
      } catch {
        update = {}
      }
      // 方向门先于口令校验，与服务端同序。
      if (typeof update.paused !== 'boolean') return fail(route, 400, 'validation_failed', '缺少目标状态')
      if (update.paused === false && sessionRole !== 'master') {
        return fail(route, 403, 'forbidden', '仅主管理员可恢复')
      }
      if (!update.current_password) return fail(route, 400, 'validation_failed', '缺少口令')
      engineSnapshotState = { ...engineSnapshotState, paused: update.paused === true }
      return json(route, { paused: engineSnapshotState.paused })

    }

    return fail(route, 404, 'not_found', `fake-logs-api 未登记 ${url.pathname}`)
  })

  return { calls }
}

export function listTaskCalls(api: FakeLogsApi): ApiCall[] {
  return api.calls.filter((call) => call.path === '/api/admin/tasks' && call.method === 'GET')
}

export function schoolsCalls(api: FakeLogsApi): ApiCall[] {
  return api.calls.filter((call) => call.path === '/api/schools' && call.method === 'GET')
}

export function pauseCalls(api: FakeLogsApi): ApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'PUT' && call.path === '/api/admin/engine/pause',
  )
}

export function engineCalls(api: FakeLogsApi): ApiCall[] {
  return api.calls.filter((call) => call.path === '/api/admin/engine' && call.method === 'GET')
}
