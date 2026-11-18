import type { Page, Request, Route } from '@playwright/test'
import type { components } from '../../src/lib/api/schema'
import { EMPTY_ACCOUNT_SUMMARY, SESSION_CSRF, SESSION_EMAIL, type Role } from './fake-accounts-api'

type Schemas = components['schemas']
export type Notification = Schemas['Notification']

/**
 * 站内通知收件箱的假 API。
 *
 * 覆盖：
 * - `GET /api/me/notifications/unread-count`
 * - `GET /api/me/notifications?limit=20&cursor=`
 * - `POST /api/me/notifications/read`
 * - `POST /api/me/notifications/read-all`
 *
 * 角色按契约收敛：master/admin 可读写；user 一律 403。
 * 供 `notify.spec.ts` 与 `visual.spec.ts` 消费，也可为其它假 API 提供默认零计数兜底。
 */

export type NotifyApiCall = {
  method: string
  path: string
  search: string
  params: Record<string, string>
  body: unknown
}

export type NotifyFailure = {
  status: number
  code: string
  message: string
  once?: boolean
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE

function isoFrom(baseMs: number, offsetMs: number): string {
  return new Date(baseMs + offsetMs).toISOString()
}

/** 四类通知全覆盖：3 条未读 + 1 条已读，含一条超两行正文用于验证截断。 */
export function createDefaultNotifications(nowMs = Date.now()): Notification[] {
  // 昨天 18:20（按 +08:00 墙钟计算）确保覆盖「昨天 HH:mm」格式分支
  const shanghaiMs = nowMs + 8 * HOUR
  const shanghaiDate = new Date(shanghaiMs)
  const yesterdayUtc = Date.UTC(
    shanghaiDate.getUTCFullYear(),
    shanghaiDate.getUTCMonth(),
    shanghaiDate.getUTCDate() - 1,
    18 - 8,
    20,
    0,
  )
  return [
    {
      id: '00000000-0000-4000-8000-000000000001',
      category: 'credential_invalid',
      title: '账号凭据失效',
      body: '账号 138****8003 在 2026-10-31 的签到中凭据失效，已移出排期；请通知归属人更新登录密码与设备授权信息后再重新启用任务。',
      school_id: 'school-a',
      created_at: isoFrom(nowMs, -5 * MINUTE),
      read_at: null,
    },
    {
      id: '00000000-0000-4000-8000-000000000002',
      category: 'review_pending',
      title: '账号待审核',
      body: '账号 138****8001 已提交审核，请及时处理。',
      school_id: 'school-a',
      created_at: isoFrom(nowMs, -24 * MINUTE),
      read_at: null,
    },
    {
      id: '00000000-0000-4000-8000-000000000003',
      category: 'capacity_rejected',
      title: '容量不足',
      body: '2026-10-31 的计划需求 26、容量 20，缺口 6。瓶颈：workers。建议 YIBAN_WORKERS=4。',
      school_id: null,
      created_at: new Date(yesterdayUtc).toISOString(),
      read_at: null,
    },
    {
      id: '00000000-0000-4000-8000-000000000004',
      category: 'sign_failed_daily',
      title: '每日签到失败摘要',
      body: '昨日共有 1 个账号签到失败，已归档至运行记录供复查。',
      school_id: 'school-b',
      created_at: isoFrom(nowMs, -3 * 24 * HOUR),
      read_at: isoFrom(nowMs, -2 * 24 * HOUR),
    },
  ]
}

export function visualNotifications(nowMs: number): Notification[] {
  return createDefaultNotifications(nowMs)
}

export type MockNotifyOptions = {
  role: Role | null
  /** 列表夹具；若提供 `pages` 则优先按游标分页返回。 */
  notifications?: Notification[]
  /** 按 cursor 键分页：如 { '': [...], 'c2': [...] }。 */
  pages?: Record<string, Notification[]>
  nextCursors?: Record<string, string>
  /** 初始未读总数覆写；缺省按未读条目实时推导。 */
  unreadCount?: number
  unreadError?: NotifyFailure
  listError?: NotifyFailure
  listDelayMs?: number
  readError?: NotifyFailure
  readAllError?: NotifyFailure
}

export type FakeNotifyApi = {
  calls: NotifyApiCall[]
  setUnreadCount: (count: number) => void
  setNotifications: (items: Notification[]) => void
  setListError: (failure?: NotifyFailure) => void
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-notify-api' }, trace_id: 'trace-notify' },
  })
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

function fail(route: Route, status: number, code: string, message: string) {
  return route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })
}

async function requireNotifyManager(route: Route, sessionRole: Role | null): Promise<boolean> {
  if (!sessionRole) {
    await fail(route, 401, 'unauthorized', '会话失效')
    return false
  }
  if (sessionRole !== 'master' && sessionRole !== 'admin') {
    await fail(route, 403, 'forbidden', '仅 master/admin 可访问站内通知')
    return false
  }
  return true
}

/**
 * 其它页面假 API 共用的通知默认桩：
 * 未登录返回 401，user 返回 403，master/admin 返回 0 条未读（验证无请求或兜底一致）。
 */
export async function handleDefaultNotifyRoute(
  route: Route,
  request: Request,
  url: URL,
  sessionRole: Role | null,
): Promise<boolean> {
  if (!url.pathname.startsWith('/api/me/notifications')) return false
  if (!(await requireNotifyManager(route, sessionRole))) return true
  if (url.pathname === '/api/me/notifications/unread-count' && request.method() === 'GET') {
    await json(route, { count: 0 })
    return true
  }
  if (url.pathname === '/api/me/notifications' && request.method() === 'GET') {
    await json(route, { items: [], next_cursor: '' })
    return true
  }
  if (
    (url.pathname === '/api/me/notifications/read' ||
      url.pathname === '/api/me/notifications/read-all') &&
    request.method() === 'POST'
  ) {
    await route.fulfill({ status: 204 })
    return true
  }
  return false
}

export async function mockNotifyApi(
  page: Page,
  options: MockNotifyOptions,
): Promise<FakeNotifyApi> {
  const calls: NotifyApiCall[] = []
  const sessionRole = options.role
  let listItems: Notification[] = (options.notifications ?? createDefaultNotifications()).map(
    (item) => ({ ...item }),
  )
  const pagedItems: Record<string, Notification[]> | null = options.pages
    ? Object.fromEntries(
        Object.entries(options.pages).map(([k, v]) => [k, v.map((item) => ({ ...item }))]),
      )
    : null
  let explicitUnread = options.unreadCount
  const pendingErrors = {
    unread: options.unreadError ? { ...options.unreadError } : undefined,
    list: options.listError ? { ...options.listError } : undefined,
    read: options.readError ? { ...options.readError } : undefined,
    readAll: options.readAllError ? { ...options.readAllError } : undefined,
  }

  const takeFailure = (key: keyof typeof pendingErrors): NotifyFailure | undefined => {
    const failure = pendingErrors[key]
    if (!failure) return undefined
    if (failure.once) pendingErrors[key] = undefined
    return failure
  }

  const allItems = (): Notification[] =>
    pagedItems ? Object.values(pagedItems).flat() : listItems

  const currentUnread = (): number => {
    if (explicitUnread !== undefined) return explicitUnread
    return allItems().filter((item) => item.read_at === null).length
  }

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body: unknown = null
    const rawBody = request.postData()
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
      return json(route, EMPTY_ACCOUNT_SUMMARY)
    }

    if (url.pathname === '/api/me/notifications/unread-count' && request.method() === 'GET') {
      if (!(await requireNotifyManager(route, sessionRole))) return
      const failure = takeFailure('unread')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      return json(route, { count: currentUnread() })
    }

    if (url.pathname === '/api/me/notifications' && request.method() === 'GET') {
      if (!(await requireNotifyManager(route, sessionRole))) return
      if (options.listDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.listDelayMs))
      }
      const failure = takeFailure('list')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      const cursor = url.searchParams.get('cursor') ?? ''
      const items = pagedItems ? (pagedItems[cursor] ?? []) : listItems
      const next_cursor = options.nextCursors?.[cursor] ?? ''
      return json(route, { items, next_cursor })
    }

    if (url.pathname === '/api/me/notifications/read' && request.method() === 'POST') {
      if (!(await requireNotifyManager(route, sessionRole))) return
      const failure = takeFailure('read')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      const ids = Array.isArray((body as { ids?: unknown } | null)?.ids)
        ? ((body as { ids: string[] }).ids ?? [])
        : []
      const nowIso = new Date().toISOString()
      let changed = 0
      for (const item of allItems()) {
        if (ids.includes(item.id) && item.read_at === null) {
          item.read_at = nowIso
          changed += 1
        }
      }
      if (explicitUnread !== undefined) {
        explicitUnread = Math.max(0, explicitUnread - changed)
      }
      return route.fulfill({ status: 204 })
    }

    if (url.pathname === '/api/me/notifications/read-all' && request.method() === 'POST') {
      if (!(await requireNotifyManager(route, sessionRole))) return
      const failure = takeFailure('readAll')
      if (failure) return fail(route, failure.status, failure.code, failure.message)
      const nowIso = new Date().toISOString()
      for (const item of allItems()) {
        if (item.read_at === null) item.read_at = nowIso
      }
      if (explicitUnread !== undefined) {
        explicitUnread = 0
      }
      return route.fulfill({ status: 204 })
    }

    return fail(route, 404, 'not_found', `fake-notify-api 未登记 ${url.pathname}`)
  })

  return {
    calls,
    setUnreadCount: (count) => {
      explicitUnread = count
    },
    setNotifications: (items) => {
      listItems = items.map((item) => ({ ...item }))
    },
    setListError: (failure) => {
      pendingErrors.list = failure ? { ...failure } : undefined
    },
  }
}

export function unreadCountCalls(api: FakeNotifyApi): NotifyApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'GET' && call.path === '/api/me/notifications/unread-count',
  )
}

export function notificationListCalls(api: FakeNotifyApi): NotifyApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'GET' && call.path === '/api/me/notifications',
  )
}

export function markReadCalls(api: FakeNotifyApi): NotifyApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === '/api/me/notifications/read',
  )
}

export function markAllReadCalls(api: FakeNotifyApi): NotifyApiCall[] {
  return api.calls.filter(
    (call) => call.method === 'POST' && call.path === '/api/me/notifications/read-all',
  )
}
