/**
 * **功能**
 * 账号域桩：类型别名、路径表、列表查询。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`。**不在此重写契约字段**。
 * `status` 用契约枚举，不另立字符串联合。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type AccountStatus = Schemas['AccountStatus']
export type Account = Schemas['Account']
export type AccountPage = Schemas['AccountPage']

/** 查询串视图。字段与 `GET /api/accounts` 的参数一一对应。 */
export type AccountQuery = {
  school_id?: string
  status?: AccountStatus
  q?: string
  cursor?: string
}

export const ACCOUNT_PATHS = {
  list: '/api/accounts',
  create: '/api/accounts',
  detail: (id: string) => `/api/accounts/${id}`,
  review: (id: string) => `/api/accounts/${id}/review`,
  pause: (id: string) => `/api/accounts/${id}/pause`,
} as const

export function listAccounts(query: AccountQuery = {}): Promise<AccountPage> {
  const params = new URLSearchParams()
  if (query.school_id) params.set('school_id', query.school_id)
  if (query.status) params.set('status', query.status)
  if (query.q) params.set('q', query.q)
  if (query.cursor) params.set('cursor', query.cursor)
  const search = params.toString()
  return apiRequest<AccountPage>(search ? `${ACCOUNT_PATHS.list}?${search}` : ACCOUNT_PATHS.list)
}
