/**
 * **功能**
 * 公告读端点：所有登录角色读同一条全站公告。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径，不含展示逻辑。
 *
 * **口径**
 * 未设置或已清空时 `text` 为空、`updated_at` 为 null；
 * 展示层据此整条隐藏，不渲染空占位。
 * 公告不是错误：读取失败由调用方静默隐藏，不弹全局错误。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type Announcement = Schemas['Announcement']

export const ANNOUNCEMENT_PATH = '/api/announcement'

export function getAnnouncement(): Promise<Announcement> {
  return apiRequest<Announcement>(ANNOUNCEMENT_PATH)
}
