/**
 * **功能**
 * 执行监控域 API：类型别名、路径与单进程快照读取。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写。
 * `GET /api/admin/engine` 只归 master/admin；单进程快照，无分页参数。
 * 行内手机号只有掩码形态；进程内三时刻未启动为 null。
 * 服务端只给租约的绝对到期时间，剩余量由页面按客户端时钟渲染。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type EngineSnapshot = Schemas['EngineSnapshot']
export type EngineInflightRow = Schemas['EngineInflightRow']
export type EngineRetryRow = Schemas['EngineRetryRow']

export const ENGINE_PATHS = {
  snapshot: '/api/admin/engine',
} as const

export function getEngineSnapshot(): Promise<EngineSnapshot> {
  return apiRequest<EngineSnapshot>(ENGINE_PATHS.snapshot)
}
