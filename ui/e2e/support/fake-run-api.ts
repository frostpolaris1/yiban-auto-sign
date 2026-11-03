import type { Page, Route } from '@playwright/test'
import type { AdminTaskRow } from '../../src/lib/api/task'
import { EMPTY_ACCOUNT_SUMMARY, SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'
import { SCHOOLS } from './fake-schools-api'

/**
 * 运行页的假 API。
 *
 * 一份路由处理 `/api/auth/me`、`/api/admin/tasks**` 与单账号诊断。
 * 响应体只用登记过的合成值：掩码手机号取 138 保留段，学校名明示合成口径。
 * 调用记录供断言「请求带哪个参数」与「无权限时发没发请求」。
 *
 * `GET /api/admin/tasks` 只归 master/admin：其余角色返回 403 信封。
 * `/api/schools` 供学校下拉取选项（master/admin）。
 * 导出按选项回 CSV 文本或失败；诊断按选项回详版或 404。
 * SSE 不在这里伪造：运行页用例经 `installFakeEventSource` 注入假体。
 */

export type ApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

/** 登记过的合成掩码号（138 保留段）。 */
export const RUN_TASKS: AdminTaskRow[] = [
  {
    id: 'task-0',
    account_id: 'account-1',
    account_phone_masked: '138****8001',
    school_id: 'school-a',
    school_name: '合成学校甲',
    task_name: '早签到',
    state: 'running',
    attempts: 1,
    result: '',
    run_at: '2026-10-31T08:10:00+08:00',
    claimed_at: '2026-10-31T08:10:00+08:00',
    updated_at: '2026-10-31T08:12:00+08:00',
  },
  {
    id: 'task-1',
    account_id: 'account-2',
    account_phone_masked: '138****8002',
    school_id: 'school-a',
    school_name: '合成学校甲',
    task_name: '早签到',
    state: 'success',
    attempts: 1,
    result: 'ok',
    run_at: '2026-10-31T08:20:00+08:00',
    claimed_at: '2026-10-31T08:20:00+08:00',
    updated_at: '2026-10-31T08:22:00+08:00',
  },
  {
    id: 'task-2',
    account_id: 'account-3',
    account_phone_masked: '138****8003',
    school_id: 'school-a',
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
    id: 'task-3',
    account_id: 'account-4',
    account_phone_masked: '138****8004',
    school_id: 'school-b',
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
    id: 'task-4',
    account_id: 'account-5',
    account_phone_masked: '138****8005',
    school_id: 'school-b',
    school_name: '合成学校乙',
    task_name: '晚自习签到',
    state: 'skipped',
    attempts: 1,
    result: 'window_missed',
    run_at: '2026-10-31T09:10:00+08:00',
    claimed_at: '2026-10-31T09:10:00+08:00',
    updated_at: '2026-10-31T09:11:00+08:00',
  },
]

/** 单账号诊断夹具：与 `AccountDiagnostics` 形状一致。 */
export const RUN_DIAGNOSTICS = {
  account_id: 'account-1',
  state: 'failed',
  run_at: '2026-10-31T08:10:00+08:00',
  attempts: 3,
  result: 'credential_invalid',
  next_retry_at: '2026-10-31T09:40:00+08:00',
  suggestion: 'wait',
  window: {
    start: '2026-10-31T08:00:00+08:00',
    end: '2026-10-31T09:20:00+08:00',
    source: 'cache',
    version: 2,
  },
  egress: { identity: 'https://eg-1' },
} as const

export type MockRunOptions = {
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
}

export type FakeRunApi = {
  calls: ApiCall[]
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-run-api' }, trace_id: 'trace-test' },
  })
}

export async function mockRunApi(page: Page, options: MockRunOptions): Promise<FakeRunApi> {
  const calls: ApiCall[] = []
  const sessionRole = options.role
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
      // 壳在每页为 master/admin 读一次；运行页不关心计数。
      return json(route, EMPTY_ACCOUNT_SUMMARY)
    }

    if (url.pathname === '/api/schools' && request.method() === 'GET') {
      // 学校下拉的选项源；服务端按角色收敛，这里全量回。
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可读学校列表')
      }
      return json(route, { items: SCHOOLS, next_cursor: '' })
    }

    if (url.pathname === '/api/admin/tasks' && request.method() === 'GET') {
      if (sessionRole !== 'master' && sessionRole !== 'admin') {
        return fail(route, 403, 'forbidden', '仅 master/admin 可查看运行页')
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

    return fail(route, 404, 'not_found', `fake-run-api 未登记 ${url.pathname}`)
  })

  return { calls }
}

export function listTaskCalls(api: FakeRunApi): ApiCall[] {
  return api.calls.filter(
    (call) => call.path === '/api/admin/tasks' && call.method === 'GET',
  )
}

/** `/api/schools` 的调用记录：断言 SSE/轮询不连带重取学校下拉。 */
export function schoolsCalls(api: FakeRunApi): ApiCall[] {
  return api.calls.filter(
    (call) => call.path === '/api/schools' && call.method === 'GET',
  )
}

/**
 * 注入可编程的假 EventSource。
 * 用例经 `window.__runES` 拿到实例：`open()` 触发建立、
 * `emit(type)` 派发业务事件、`fail()` 触发断线。
 */
export async function installFakeEventSource(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const sources: unknown[] = []
    class FakeEventSource {
      url: string
      closed = false
      onopen: ((event: Event) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      private listeners = new Map<string, (event: MessageEvent) => void>()

      constructor(url: string) {
        this.url = url
        sources.push(this)
      }
      close() {
        this.closed = true
      }
      addEventListener(type: string, listener: (event: MessageEvent) => void) {
        this.listeners.set(type, listener)
      }
      open() {
        this.onopen?.(new Event('open'))
      }
      emit(type: string) {
        this.listeners.get(type)?.(new MessageEvent(type, { data: '{}' }))
      }
      fail() {
        this.onerror?.(new Event('error'))
      }
    }
    ;(window as unknown as { __runES: unknown[] }).__runES = sources
    window.EventSource = FakeEventSource as unknown as typeof EventSource
    // 类实例经 evaluate 序列化会丢方法：动作由页面内的分发器执行。
    ;(window as unknown as { __runESAct: RunESAction }).__runESAct = (action, arg) => {
      if (action === 'count') return sources.length
      const source = sources[sources.length - 1] as RunESHandle | undefined
      if (!source) return sources.length
      if (action === 'open') source.open()
      if (action === 'fail') source.fail()
      if (action === 'emit') source.emit(String(arg))
      return sources.length
    }
  })
}

type RunESHandle = {
  open(): void
  emit(type: string): void
  fail(): void
  closed: boolean
}

type RunESAction = (action: 'count' | 'open' | 'fail' | 'emit', arg?: string) => number

async function act(page: Page, action: 'count' | 'open' | 'fail', arg?: string): Promise<number> {
  return page.evaluate(
    ([action, arg]) => (window as unknown as { __runESAct: RunESAction }).__runESAct(action, arg),
    [action, arg] as const,
  )
}

/** 当前连接数（按创建序累计，重连不重置）。 */
export async function fakeSourceCount(page: Page): Promise<number> {
  return act(page, 'count')
}

/** 让最近一条连接派发一个业务事件。 */
export async function fakeSourceEmit(page: Page, type: string): Promise<void> {
  await page.evaluate(
    ([action, arg]) => (window as unknown as { __runESAct: RunESAction }).__runESAct(action, arg),
    ['emit', type] as const,
  )
}

/** 让最近一条连接进入断线。 */
export async function fakeSourceFail(page: Page): Promise<void> {
  await act(page, 'fail')
}

/** 让最近一条连接完成建立。 */
export async function fakeSourceOpen(page: Page): Promise<void> {
  await act(page, 'open')
}
