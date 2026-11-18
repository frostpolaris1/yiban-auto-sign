/**
 * **功能**
 * 站内通知 API：未读数、游标分页列表、标记已读、全部已读。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型全部取自生成的 `schema.d.ts`（`Notification`、`NotificationPage`、`UnreadCount`）。
 * 契约：master/admin 只读自己的行；user 一律 403（前端不渲染铃铛、不发请求）。
 * 游标分页：`next_cursor` 空串代表末页；`limit` 缺省 20。
 * `read_at` 为 null 表示未读；标记接口返回 204 且幂等。
 */

import { apiRequest } from './client'
import type { components, operations } from './schema'

type Schemas = components['schemas']

export type NotifyCategory = Schemas['NotifyCategory']
export type Notification = Schemas['Notification']
export type NotificationPage = Schemas['NotificationPage']
export type UnreadCount = Schemas['UnreadCount']
export type NotificationRead = Schemas['NotificationRead']

export type NotificationQuery = NonNullable<operations['NotificationList']['parameters']['query']>

export const NOTIFICATION_PAGE_LIMIT = 20

export const NOTIFY_PATHS = {
  list: '/api/me/notifications',
  unreadCount: '/api/me/notifications/unread-count',
  read: '/api/me/notifications/read',
  readAll: '/api/me/notifications/read-all',
} as const

/** 构造列表查询串：limit 缺省为 20，空游标不进查询串。 */
export function buildNotificationSearch(query: NotificationQuery = {}): string {
  const params = new URLSearchParams()
  params.set('limit', String(query.limit ?? NOTIFICATION_PAGE_LIMIT))
  if (query.cursor) params.set('cursor', query.cursor)
  const search = params.toString()
  return search ? `?${search}` : ''
}

export function listNotifications(query: NotificationQuery = {}): Promise<NotificationPage> {
  return apiRequest<NotificationPage>(`${NOTIFY_PATHS.list}${buildNotificationSearch(query)}`)
}

export function getUnreadCount(): Promise<UnreadCount> {
  return apiRequest<UnreadCount>(NOTIFY_PATHS.unreadCount)
}

export function markNotificationsRead(ids: string[]): Promise<void> {
  const body: NotificationRead = { ids }
  return apiRequest<void>(NOTIFY_PATHS.read, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function markAllNotificationsRead(): Promise<void> {
  return apiRequest<void>(NOTIFY_PATHS.readAll, { method: 'POST' })
}
