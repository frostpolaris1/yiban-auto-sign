/**
 * **功能**
 * 站内通知 API：列表、未读数、标记已读、全部已读。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串。
 *
 * **口径**
 * 契约：master/admin 只读自己的行；user 403（无站内通知面）。
 * 游标式分页：next_cursor 空串＝末页；dedupe_key 不进响应。
 * read_at 为 null 表示未读；read 接口忽略他人的 id。
 */

import { apiRequest } from './client'
import type { components } from './schema'

type Schemas = components['schemas']

export type Notification = Schemas['Notification']
export type NotificationPage = Schemas['NotificationPage']
export type UnreadCount = Schemas['UnreadCount']

export const NOTIFY_PATHS = {
  list: '/api/me/notifications',
  unread: '/api/me/notifications/unread-count',
  read: '/api/me/notifications/read',
  readAll: '/api/me/notifications/read-all',
} as const

export function listNotifications(limit = 5): Promise<NotificationPage> {
  return apiRequest<NotificationPage>(`${NOTIFY_PATHS.list}?limit=${limit}`)
}

export function getUnreadCount(): Promise<UnreadCount> {
  return apiRequest<UnreadCount>(NOTIFY_PATHS.unread)
}

export function markNotificationsRead(ids: string[]): Promise<void> {
  return apiRequest<void>(NOTIFY_PATHS.read, {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })
}

export function markAllNotificationsRead(): Promise<void> {
  return apiRequest<void>(NOTIFY_PATHS.readAll, { method: 'POST' })
}