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
 * 批量签到与单条签到一样不要求二次口令：每项独立入队，逐项回 ok/error。
 * 彻底删除（purge）是 204 无响应体，只能作用于已软删除的行。
 */

import type { components, operations } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type AccountStatus = Schemas['AccountStatus']
export type Account = Schemas['Account']
export type AccountPage = Schemas['AccountPage']
export type AccountSummary = Schemas['AccountSummary']
export type AccountPauseRequest = Schemas['AccountPauseRequest']
export type AccountReviewRequest = Schemas['AccountReviewRequest']
export type AccountSignAccepted = Schemas['AccountSignAccepted']
export type AccountVerifyResult = Schemas['AccountVerifyResult']
export type AccountUpdate = Schemas['AccountUpdate']
export type AccountLocation = Schemas['AccountLocation']
export type AccountLocationUpdate = Schemas['AccountLocationUpdate']
export type AccountDevice = Schemas['AccountDevice']
export type AccountCreateBody = Schemas['AccountCreate']
export type AccountBatchAction = Schemas['AccountBatchAction']
export type AccountBatchRequest = Schemas['AccountBatchRequest']
export type AccountBatchItem = Schemas['AccountBatchItem']
export type AccountBatchResult = Schemas['AccountBatchResult']
export type AccountBatchSignRequest = Schemas['AccountBatchSignRequest']
export type AccountBatchSignItem = Schemas['AccountBatchSignItem']
export type AccountBatchSignResult = Schemas['AccountBatchSignResult']
export type AccountDetectRequest = Schemas['AccountDetectRequest']
export type AccountDetectResult = Schemas['AccountDetectResult']
export type AccountPersonNameResult = Schemas['AccountPersonNameResult']

/** 查询串视图。字段直接取契约生成的 `AccountList` 参数，不另写一份。 */
export type AccountQuery = NonNullable<operations['AccountList']['parameters']['query']>

export const ACCOUNT_PATHS = {
  list: '/api/accounts',
  create: '/api/accounts',
  summary: '/api/accounts/summary',
  detail: (id: string) => `/api/accounts/${encodeURIComponent(id)}`,
  review: (id: string) => `/api/accounts/${encodeURIComponent(id)}/review`,
  pause: (id: string) => `/api/accounts/${encodeURIComponent(id)}/pause`,
  verify: (id: string) => `/api/accounts/${encodeURIComponent(id)}/verify`,
  sign: (id: string) => `/api/accounts/${encodeURIComponent(id)}/sign`,
  location: (id: string) => `/api/accounts/${encodeURIComponent(id)}/location`,
  deviceRefresh: (id: string) => `/api/accounts/${encodeURIComponent(id)}/device-refresh`,
  restore: (id: string) => `/api/accounts/${encodeURIComponent(id)}/restore`,
  detect: '/api/accounts/detect',
  personName: (id: string) =>
    `/api/accounts/${encodeURIComponent(id)}/person-name`,
  selfDelete: (id: string) => `/api/me/accounts/${encodeURIComponent(id)}/delete`,
  selfRestore: (id: string) => `/api/me/accounts/${encodeURIComponent(id)}/restore`,
  batch: '/api/accounts/batch',
  batchSign: '/api/accounts/batch/sign',
  purge: (id: string) => `/api/accounts/${encodeURIComponent(id)}/purge`,
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
  if (query.include_deleted) params.set('include_deleted', 'true')
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return search ? `?${search}` : ''
}

export function listAccounts(query: AccountQuery = {}): Promise<AccountPage> {
  return apiRequest<AccountPage>(`${ACCOUNT_PATHS.list}${buildAccountSearch(query)}`)
}

/**
 * 待处理账号计数。只数未软删账号，与列表缺省口径同源；
 * 仅 master/admin，user 调用收到 403。
 */
export function getAccountSummary(): Promise<AccountSummary> {
  return apiRequest<AccountSummary>(ACCOUNT_PATHS.summary)
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

/** 位置策略读面：user 读本人，admin 读绑定学校；缺省读 auto。 */
export function getAccountLocation(id: string): Promise<AccountLocation> {
  return apiRequest<AccountLocation>(ACCOUNT_PATHS.location(id))
}

export function setAccountLocation(
  id: string,
  body: AccountLocationUpdate,
): Promise<AccountLocation> {
  return apiRequest<AccountLocation>(ACCOUNT_PATHS.location(id), {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

/**
 * 204 软删除。契约要求 `current_password`；管理员入口可留空，
 * 服务端会以 400 拒绝，调用方必须补二次口令后再重试。
 */
export function deleteAccount(id: string, currentPassword?: string): Promise<void> {
  return apiRequest<void>(ACCOUNT_PATHS.detail(id), {
    method: 'DELETE',
    body:
      currentPassword === undefined
        ? undefined
        : JSON.stringify({ current_password: currentPassword }),
  })
}

/** 本人软删自己的签到账号；foreign 账号 404，不泄露存在性。 */
export function selfDeleteAccount(id: string, currentPassword: string): Promise<void> {
  return apiRequest<void>(ACCOUNT_PATHS.selfDelete(id), {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword }),
  })
}

/** 建号。`owner_user_id` 只给 master/admin；指定属主必须是活跃用户。 */
export function createAccount(body: AccountCreateBody): Promise<Account> {
  return apiRequest<Account>(ACCOUNT_PATHS.create, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 恢复软删账号。只归 master/admin；行保持删除前状态。 */
export function restoreAccount(id: string, currentPassword?: string): Promise<Account> {
  return apiRequest<Account>(ACCOUNT_PATHS.restore(id), {
    method: 'POST',
    body:
      currentPassword === undefined
        ? undefined
        : JSON.stringify({ current_password: currentPassword }),
  })
}

/** 本人恢复自己删掉的签到账号；只认 deleted_by=本人，过期 409。 */
export function selfRestoreAccount(id: string, currentPassword: string): Promise<Account> {
  return apiRequest<Account>(ACCOUNT_PATHS.selfRestore(id), {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword }),
  })
}

/**
 * 设备刷新。响应只给掩码、机型与授权标志；明文设备码从不出服务端。
 * 502 表示平台失败，零写库。契约要求 `current_password`。
 */
export function refreshDevice(id: string, currentPassword?: string): Promise<AccountDevice> {
  return apiRequest<AccountDevice>(ACCOUNT_PATHS.deviceRefresh(id), {
    method: 'POST',
    body:
      currentPassword === undefined
        ? undefined
        : JSON.stringify({ current_password: currentPassword }),
  })
}

/**
 * 建号前探测平台身份：一次认证后读回大学/学校投影/机型/设备码存在位。
 * 不落库、不回显口令；失败 502 中性信封，速率限制按 IP + 手机号。
 */
export function detectAccount(body: AccountDetectRequest): Promise<AccountDetectResult> {
  return apiRequest<AccountDetectResult>(ACCOUNT_PATHS.detect, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 按需读取平台姓名：不落库、不写日志，与 verify/sign 共用一个限流桶。 */
export function getPersonName(id: string): Promise<AccountPersonNameResult> {
  return apiRequest<AccountPersonNameResult>(ACCOUNT_PATHS.personName(id), {
    method: 'POST',
  })
}

/**
 * 批量动作：同一动作作用于 1..10 个账号。
 * 每项独立事务；响应逐项给 ok/error，顺序与请求 ids 一致。
 * 二次口令在请求体里必填：缺值 400、口令错 403、连续失败 429。
 */
export function batchAccounts(body: AccountBatchRequest): Promise<AccountBatchResult> {
  return apiRequest<AccountBatchResult>(ACCOUNT_PATHS.batch, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/**
 * 批量签到：一次入队 1..10 个账号，只归 master/admin。
 * 与单条签到一致，**不要二次口令**。
 * 空数组、超过 10 个或 ids 重复由服务端整批拒绝（400），一项都不入队；
 * 通过校验后逐项独立入队，某项失败不阻断后续、也不回滚已入队的项。
 * 逐项错误码：不存在/越权 not_found、id 不是 UUID validation_failed、
 * 账号非 active conflict，以及引擎侧的 already_signed / sign_running /
 * window_closed / capacity_exceeded / rate_limited（引擎全局暂停时
 * message 是 engine is paused）。ok 项只带 task_id，不等待执行结果。
 */
export function batchSignAccounts(body: AccountBatchSignRequest): Promise<AccountBatchSignResult> {
  return apiRequest<AccountBatchSignResult>(ACCOUNT_PATHS.batchSign, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/**
 * 彻底删除（硬删）：只能删已软删除的账号，凭据与签到任务一起物理删除。
 * 审计行永久保留。目标未先软删 409；不存在或越权 404；成功 204 无响应体。
 * 契约要求 `current_password`：缺值 400、口令错 403、连续失败 429，被拒零写入。
 */
export function purgeAccount(id: string, currentPassword: string): Promise<void> {
  return apiRequest<void>(ACCOUNT_PATHS.purge(id), {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword }),
  })
}
