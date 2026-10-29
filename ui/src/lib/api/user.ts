/**
 * **功能**
 * 用户域 API：类型别名、路径表与列表查询。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写。
 * `GET /api/users` 只归 master，分页是游标式：末页 `next_cursor` 是空串。
 * 查询串只带非空值。空串与 undefined 都不出现。
 * 本页只读：建号（`POST /api/users`）不在本文件提供。
 */

import type { components, operations } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type User = Schemas['User']
export type UserRole = Schemas['UserRole']
export type UserStatus = Schemas['UserStatus']
export type UserPage = Schemas['UserPage']

/** 查询串视图。字段直接取契约生成的 `UserList` 参数，不另写一份。 */
export type UserQuery = NonNullable<operations['UserList']['parameters']['query']>

export const USER_PATHS = {
  list: '/api/users',
} as const

export function buildUserSearch(query: UserQuery = {}): string {
  const params = new URLSearchParams()
  if (query.role) params.set('role', query.role)
  if (query.status) params.set('status', query.status)
  if (query.q) params.set('q', query.q)
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return search ? `?${search}` : ''
}

export function listUsers(query: UserQuery = {}): Promise<UserPage> {
  return apiRequest<UserPage>(`${USER_PATHS.list}${buildUserSearch(query)}`)
}
