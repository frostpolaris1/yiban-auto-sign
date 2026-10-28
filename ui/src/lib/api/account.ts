/**
 * **功能**
 * 账号域 API：类型别名、路径表、列表查询、详情与全部写操作。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`。**不在此重写契约字段**。
 * `status` 用契约枚举，不另立字符串联合。
 * 写操作走 `apiRequest`，由它附 CSRF 头并抛 `ApiError` 信封。
 */

import type { components, operations } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type AccountStatus = Schemas['AccountStatus']
export type Account = Schemas['Account']
export type AccountPage = Schemas['AccountPage']
export type AccountPauseRequest = Schemas['AccountPauseRequest']
export type AccountReviewRequest = Schemas['AccountReviewRequest']
export type AccountSignAccepted = Schemas['AccountSignAccepted']
export type AccountVerifyResult = Schemas['AccountVerifyResult']
export type AccountUpdate = Schemas['AccountUpdate']
export type AccountLocation = Schemas['AccountLocation']
export type AccountLocationUpdate = Schemas['AccountLocationUpdate']

/** 查询串视图。字段直接取契约生成的 `AccountList` 参数，不另写一份。 */
export type AccountQuery = NonNullable<operations['AccountList']['parameters']['query']>

export const ACCOUNT_PATHS = {
  list: '/api/accounts',
  create: '/api/accounts',
  detail: (id: string) => `/api/accounts/${encodeURIComponent(id)}`,
  review: (id: string) => `/api/accounts/${encodeURIComponent(id)}/review`,
  pause: (id: string) => `/api/accounts/${encodeURIComponent(id)}/pause`,
  verify: (id: string) => `/api/accounts/${encodeURIComponent(id)}/verify`,
  sign: (id: string) => `/api/accounts/${encodeURIComponent(id)}/sign`,
  location: (id: string) => `/api/accounts/${encodeURIComponent(id)}/location`,
} as const

/**
 * 查询串只带非空值。空串与 undefined 都不出现。
 * 测试解析参数后按名比较，不钉字符顺序。
 */
export function buildAccountSearch(query: AccountQuery = {}): string {
  const params = new URLSearchParams()
  if (query.school_id) params.set('school_id', query.school_id)
  if (query.status) params.set('status', query.status)
  if (query.q) params.set('q', query.q)
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return search ? `?${search}` : ''
}

export function listAccounts(query: AccountQuery = {}): Promise<AccountPage> {
  return apiRequest<AccountPage>(`${ACCOUNT_PATHS.list}${buildAccountSearch(query)}`)
}

export function getAccount(id: string): Promise<Account> {
  return apiRequest<Account>(ACCOUNT_PATHS.detail(id))
}

/** `paused: true` 暂停；`paused: false` 恢复。服务端按角色决定暂停种类。 */
export function pauseAccount(id: string, body: AccountPauseRequest): Promise<Account> {
  return apiRequest<Account>(ACCOUNT_PATHS.pause(id), {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function signAccount(id: string): Promise<AccountSignAccepted> {
  return apiRequest<AccountSignAccepted>(ACCOUNT_PATHS.sign(id), { method: 'POST' })
}

export function verifyAccount(id: string): Promise<AccountVerifyResult> {
  return apiRequest<AccountVerifyResult>(ACCOUNT_PATHS.verify(id), { method: 'POST' })
}

export function reviewAccount(id: string, body: AccountReviewRequest): Promise<Account> {
  return apiRequest<Account>(ACCOUNT_PATHS.review(id), {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** PATCH 只带填写字段。school 变更在 P1 被 409，表单不提供。 */
export function updateAccount(id: string, body: AccountUpdate): Promise<Account> {
  return apiRequest<Account>(ACCOUNT_PATHS.detail(id), {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

/** 位置策略没有读端点：成功响应由调用方存会话内缓存。 */
export function setAccountLocation(
  id: string,
  body: AccountLocationUpdate,
): Promise<AccountLocation> {
  return apiRequest<AccountLocation>(ACCOUNT_PATHS.location(id), {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

/** 204 软删除。成功后无响应体。 */
export function deleteAccount(id: string): Promise<void> {
  return apiRequest<void>(ACCOUNT_PATHS.detail(id), { method: 'DELETE' })
}
