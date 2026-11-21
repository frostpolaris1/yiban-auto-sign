/**
 * **功能**
 * 公开存活探针 `GET /healthz` 的客户端与 Query 键。
 *
 * **归属**
 * `lib/api` 契约适配层：无状态请求函数；类型来自 `schema.d.ts`。
 *
 * **口径**
 * 契约标注该端点 `security: []`、`x-roles: [public]`：无会话、无 CSRF、不触库。
 * 它是系统设置页「版本与自检」分区唯一可得的实时自检来源；
 * 响应只有 `{ status: "ok" }`，不携带任何版本字段。
 */

import { apiRequest } from './client'
import type { components } from './schema.d'

export type HealthStatus = components['schemas']['HealthStatus']

export const HEALTH_QUERY_KEY = ['health'] as const

export function getHealthStatus(): Promise<HealthStatus> {
  return apiRequest<HealthStatus>('/healthz')
}
