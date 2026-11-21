/**
 * **功能**
 * 审计链巡检与审计台账 API：类型别名、路径表、查询串与两个只读查询。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写契约形状。
 * 端点 `x-roles` 只有 master/admin；user 调用收到 403。
 * 断链与空链都是 200 响应里的巡检结论，不是 HTTP 错误；
 * 展示层按 `ok`/`empty`/`broken_at` 三态渲染，不把结论包成异常。
 * 审计列表是游标分页：末页 `next_cursor` 是空串。查询串只带非空值。
 */

import type { components, operations } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type AuditStatusReport = Schemas['AuditStatusReport']
export type AuditLogItem = Schemas['AuditLogItem']
export type AuditLogPage = Schemas['AuditLogPage']

/** 列表查询视图。字段直接取契约生成的 `AuditLogList` 参数，不另写一份。 */
export type AuditQuery = NonNullable<operations['AuditLogList']['parameters']['query']>

export const AUDIT_PATHS = {
  status: '/api/admin/audit-status',
  list: '/api/audit-logs',
} as const

export function buildAuditSearch(query: AuditQuery = {}): string {
  const params = new URLSearchParams()
  if (query.action) params.set('action', query.action)
  if (query.actor) params.set('actor', query.actor)
  if (query.target) params.set('target', query.target)
  if (query.from) params.set('from', query.from)
  if (query.to) params.set('to', query.to)
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return search ? `?${search}` : ''
}

/** 按需复算整条审计链；响应不含审计正文。 */
export function getAuditStatus(): Promise<AuditStatusReport> {
  return apiRequest<AuditStatusReport>(AUDIT_PATHS.status)
}

/** 审计台账一页。筛选与游标都走契约参数，详情已由服务端脱敏。 */
export function listAuditLogs(query: AuditQuery = {}): Promise<AuditLogPage> {
  return apiRequest<AuditLogPage>(`${AUDIT_PATHS.list}${buildAuditSearch(query)}`)
}
