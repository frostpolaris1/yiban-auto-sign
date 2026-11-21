/**
 * **功能**
 * 全站公告读写接口客户端。
 *
 * **归属**
 * `lib/api` 契约适配层：读走 `/api/announcement`，写走 `/api/admin/announcement`。
 *
 * **口径**
 * 写入正文的裁剪、单行与 200 字上限校验由特性纯函数先做前置检查，服务端仍是最终权威；
 * 传空串即清空公告并使 `updated_at` 回到 `null`。
 */

import { apiRequest } from './client'
import type { components } from './schema.d'

export type Announcement = components['schemas']['Announcement']
export type AnnouncementUpdate = components['schemas']['AnnouncementUpdate']

export const ANNOUNCEMENT_QUERY_KEY = ['announcement'] as const

export function getAnnouncement(): Promise<Announcement> {
  return apiRequest<Announcement>('/api/announcement')
}

export function updateAnnouncement(body: AnnouncementUpdate): Promise<Announcement> {
  return apiRequest<Announcement>('/api/admin/announcement', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}
