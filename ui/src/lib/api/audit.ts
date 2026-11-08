/**
 * **功能**
 * 审计链巡检 API：类型别名、路径表与一个只读查询。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写契约形状。
 * 端点 `x-roles` 只有 master/admin；user 调用收到 403。
 * 断链与空链都是 200 响应里的巡检结论，不是 HTTP 错误；
 * 展示层按 `ok`/`empty`/`broken_at` 三态渲染，不把结论包成异常。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type AuditStatusReport = Schemas['AuditStatusReport']

export const AUDIT_PATHS = {
  status: '/api/admin/audit-status',
} as const

/** 按需复算整条审计链；响应不含审计正文。 */
export function getAuditStatus(): Promise<AuditStatusReport> {
  return apiRequest<AuditStatusReport>(AUDIT_PATHS.status)
}
