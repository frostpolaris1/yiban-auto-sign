import type { Page } from '@playwright/test'
import type { AdminTaskRow } from '../../src/lib/api/task'

/**
 * 签到事件页签的假 API 底料。
 *
 * 任务与诊断夹具只用登记过的合成值：掩码手机号取 138 保留段，学校名明示合成口径。
 * 路由与调用记录由 `fake-logs-api` 组合登记，`/logs` 用例只面对那份组合假体。
 * SSE 在这里伪造：用例经 `installFakeEventSource` 注入假体，断言经 fakeSource* 驱动。
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
  display_name: '账号0001',
  phone_masked: '138****0001',
  account_status: 'active',
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

export type FakeRunApi = {
  calls: ApiCall[]
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
 * 每个 MessageEvent 都带 lastEventId；`end` 帧到达后实例标记为已关闭。
 */
export async function installFakeEventSource(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const sources: unknown[] = []
    class FakeEventSource {
      url: string
      closed = false
      closeCalls = 0
      onopen: ((event: Event) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      private listeners = new Map<string, (event: MessageEvent) => void>()

      constructor(url: string) {
        this.url = url
        sources.push(this)
      }
      close() {
        this.closed = true
        this.closeCalls += 1
      }
      addEventListener(type: string, listener: (event: MessageEvent) => void) {
        this.listeners.set(type, listener)
      }
      open() {
        this.onopen?.(new Event('open'))
      }
      emit(type: string, data = '{}', id = '') {
        this.listeners.get(type)?.(new MessageEvent(type, { data, lastEventId: id }))
      }
      fail() {
        this.onerror?.(new Event('error'))
      }
    }
    ;(window as unknown as { __runES: unknown[] }).__runES = sources
    window.EventSource = FakeEventSource as unknown as typeof EventSource
    // 类实例经 evaluate 序列化会丢方法：动作由页面内的分发器执行。
    // 动作参数整体走 JSON：帧的 data 由调用方给对象，序列化后才上 MessageEvent。
    ;(window as unknown as { __runESAct: (action: RunESAction) => number }).__runESAct = (
      action,
    ) => {
      if (action.action === 'count') return sources.length
      const source = sources[sources.length - 1] as RunESHandle | undefined
      if (!source) return sources.length
      if (action.action === 'open') source.open()
      if (action.action === 'fail') source.fail()
      if (action.action === 'emit') {
        source.emit(
          action.frame.type,
          action.frame.data === undefined ? '{}' : JSON.stringify(action.frame.data),
          action.frame.id ?? '',
        )
      }
      if (action.action === 'heartbeat') source.emit('heartbeat')
      if (action.action === 'resync') {
        source.emit('resync', JSON.stringify({ from: action.from }), action.id ?? '')
      }
      if (action.action === 'snapshot') {
        source.emit(
          'snapshot',
          action.data === undefined ? '{}' : JSON.stringify(action.data),
          action.id ?? '',
        )
      }
      if (action.action === 'end') {
        source.emit('end', JSON.stringify(action.reason ? { reason: action.reason } : {}), '')
        // 服务端写完 end 即关闭连接。
        source.closed = true
      }
      return sources.length
    }
  })
}

/** 假事件源可派发的单帧；data 走 JSON 序列化，id 进 lastEventId。 */
export type FakeEventFrame = {
  type: string
  data?: unknown
  id?: string
}

type RunESHandle = {
  open(): void
  emit(type: string, data?: string, id?: string): void
  fail(): void
  closed: boolean
  closeCalls: number
}

type RunESAction =
  | { action: 'count' }
  | { action: 'open' }
  | { action: 'fail' }
  | { action: 'emit'; frame: FakeEventFrame }
  | { action: 'heartbeat' }
  | { action: 'resync'; from: number; id?: string }
  | { action: 'snapshot'; data?: unknown; id?: string }
  | { action: 'end'; reason?: string }

async function act(page: Page, action: RunESAction): Promise<number> {
  return page.evaluate(
    (payload) =>
      (window as unknown as { __runESAct: (action: RunESAction) => number }).__runESAct(payload),
    action,
  )
}

/** 当前连接数（按创建序累计，重连不重置）。 */
export async function fakeSourceCount(page: Page): Promise<number> {
  return act(page, { action: 'count' })
}

/**
 * 让最近一条连接派发一个业务事件。
 * 字符串形态保留原签名；对象形态可带 id 与 data。
 */
export async function fakeSourceEmit(
  page: Page,
  event: string | FakeEventFrame,
): Promise<void> {
  const frame = typeof event === 'string' ? { type: event } : event
  await act(page, { action: 'emit', frame })
}

/** 让最近一条连接派发心跳（15 秒一次的保活帧）。 */
export async function fakeSourceHeartbeat(page: Page): Promise<void> {
  await act(page, { action: 'heartbeat' })
}

/** 让最近一条连接派发 resync；from 是客户端落后的位置。 */
export async function fakeSourceResync(page: Page, from: number, id?: string): Promise<void> {
  await act(page, { action: 'resync', from, id })
}

/** 让最近一条连接派发 snapshot（resync 后现算的当日前景）。 */
export async function fakeSourceSnapshot(
  page: Page,
  data?: unknown,
  id?: string,
): Promise<void> {
  await act(page, { action: 'snapshot', data, id })
}

/** 让最近一条连接派发 end；reason 进 data.reason。 */
export async function fakeSourceEnd(page: Page, reason?: string): Promise<void> {
  await act(page, { action: 'end', reason })
}

/** 最近一条连接是否已关闭（end 帧或客户端 close 都会置位）。 */
export async function fakeSourceClosed(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const sources = (window as unknown as { __runES: { closed: boolean }[] }).__runES
    return sources[sources.length - 1]?.closed ?? false
  })
}

/** 最近一条连接被 close() 的次数：证明是客户端主动关闭，而非帧自带的关闭。 */
export async function fakeSourceCloseCalls(page: Page): Promise<number> {
  return page.evaluate(() => {
    const sources = (window as unknown as { __runES: { closeCalls: number }[] }).__runES
    return sources[sources.length - 1]?.closeCalls ?? 0
  })
}

/** 让最近一条连接进入断线。 */
export async function fakeSourceFail(page: Page): Promise<void> {
  await act(page, { action: 'fail' })
}

/** 让最近一条连接完成建立。 */
export async function fakeSourceOpen(page: Page): Promise<void> {
  await act(page, { action: 'open' })
}
