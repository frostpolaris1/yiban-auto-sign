/**
 * **功能**
 * 用户域 API：类型别名、路径表与全部用户端点。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写。
 * `GET /api/users` 只归 master，分页是游标式：末页 `next_cursor` 是空串。
 * `include_deleted` 打开时软删行也回，行内带 `deleted_at`。
 * lookup 归 master/admin：只回 id、掩码邮箱、状态；`q` 必填，无游标。
 * `temp_password` 只在 `resetUserPassword` 的响应出现一次，本层不留副本。
 * 查询串只带非空值。空串与 undefined 都不出现。
 */

import type { components, operations } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type User = Schemas['User']
export type UserRole = Schemas['UserRole']
export type UserStatus = Schemas['UserStatus']
export type UserPage = Schemas['UserPage']
export type UserUpdateBody = Schemas['UserUpdate']
export type UserTempPassword = Schemas['UserTempPassword']
export type UserLookupRef = Schemas['UserLookupRef']
export type UserLookupPage = Schemas['UserLookupPage']
export type UserCreateBody = Schemas['UserCreate']

/** 查询串视图。字段直接取契约生成的 `UserList` 参数，不另写一份。 */
export type UserQuery = NonNullable<operations['UserList']['parameters']['query']>

export const USER_PATHS = {
  list: '/api/users',
  lookup: '/api/users/lookup',
} as const

export function buildUserSearch(query: UserQuery = {}): string {
  const params = new URLSearchParams()
  if (query.role) params.set('role', query.role)
  if (query.status) params.set('status', query.status)
  if (query.q) params.set('q', query.q)
  if (query.include_deleted) params.set('include_deleted', 'true')
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return search ? `?${search}` : ''
}

export function listUsers(query: UserQuery = {}): Promise<UserPage> {
  return apiRequest<UserPage>(`${USER_PATHS.list}${buildUserSearch(query)}`)
}

/** 建号。409 表示活跃用户邮箱重复。 */
export function createUser(body: UserCreateBody): Promise<User> {
  return apiRequest<User>(USER_PATHS.list, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 改角色或状态。409 表示最后一名未删 master 不许降级或停用。 */
export function updateUser(id: string, body: UserUpdateBody): Promise<User> {
  return apiRequest<User>(`${USER_PATHS.list}/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

/** 重置口令。响应里的 temp_password 单次可见。 */
export function resetUserPassword(id: string): Promise<UserTempPassword> {
  return apiRequest<UserTempPassword>(`${USER_PATHS.list}/${id}/password-reset`, {
    method: 'POST',
  })
}

/** 软删。409 表示名下还有活跃账号。 */
export function deleteUser(id: string): Promise<void> {
  return apiRequest<void>(`${USER_PATHS.list}/${id}`, { method: 'DELETE' })
}

/** 恢复软删用户。409 邮箱被占用；429 活跃用户容量满。 */
export function restoreUser(id: string): Promise<User> {
  return apiRequest<User>(`${USER_PATHS.list}/${id}/restore`, { method: 'POST' })
}

/** 归属选择器的受限查询。q 必填；只回掩码邮箱。 */
export function lookupUsers(q: string, limit?: number): Promise<UserLookupPage> {
  const params = new URLSearchParams({ q })
  if (limit) params.set('limit', String(limit))
  return apiRequest<UserLookupPage>(`${USER_PATHS.lookup}?${params.toString()}`)
}
