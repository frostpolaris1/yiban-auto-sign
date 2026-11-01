/**
 * **功能**
 * 运行页任务域 API：类型别名、路径表与检索、CSV 导出、单账号诊断。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写。
 * `GET /api/admin/tasks` 只归 master/admin，分页是游标式：
 * 末页 `next_cursor` 是空串。行内手机号只有掩码形态。
 * `exportTasksCsv` 不走 `apiRequest`：导出是二进制附件，
 * 由调用方拿响应头与 blob；非 2xx 时按错误信封抛 `ApiError`。
 * 查询串只带非空值。空串与 undefined 都不出现。
 */

import type { components, operations } from './schema'
import { ApiError, apiRequest, getCsrfToken } from './client'

type Schemas = components['schemas']

export type AdminTaskRow = Schemas['AdminTaskRow']
export type TaskPage = Schemas['TaskPage']
export type AccountDiagnostics = Schemas['AccountDiagnostics']

/** 查询串视图。字段直接取契约生成的 `AdminTaskList` 参数，不另写一份。 */
export type TaskQuery = NonNullable<operations['AdminTaskList']['parameters']['query']>

export const TASK_PATHS = {
  list: '/api/admin/tasks',
  export: '/api/admin/tasks/export',
  accountDiagnostics: (id: string) => `/api/accounts/${id}/diagnostics`,
} as const

export function buildTaskSearch(query: TaskQuery = {}): string {
  const params = new URLSearchParams()
  if (query.day) params.set('day', query.day)
  if (query.state) params.set('state', query.state)
  if (query.school_id) params.set('school_id', query.school_id)
  if (query.q) params.set('q', query.q)
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return search ? `?${search}` : ''
}

export function listTasks(query: TaskQuery = {}): Promise<TaskPage> {
  return apiRequest<TaskPage>(`${TASK_PATHS.list}${buildTaskSearch(query)}`)
}

/**
 * 下载 CSV 导出。同筛选同口径；响应头 `X-Export-Truncated: true`
 * 表示超过 10000 行被截断。失败按错误信封抛 `ApiError`。
 */
export async function exportTasksCsv(
  query: Omit<TaskQuery, 'cursor' | 'limit'> = {},
): Promise<{ blob: Blob; truncated: boolean; filename: string }> {
  const response = await fetch(`${TASK_PATHS.export}${buildTaskSearch(query)}`, {
    credentials: 'include',
    headers: {
      Accept: 'text/csv',
      ...(getCsrfToken() ? { 'X-CSRF-Token': getCsrfToken() as string } : {}),
    },
  })
  if (!response.ok) {
    let payload = { error: { code: 'internal_error', message: `请求失败（HTTP ${response.status}）` } }
    try {
      const body = (await response.json()) as typeof payload
      if (body.error) payload = body
    } catch {
      /* 响应体不是 JSON 时保留兜底错误，状态码仍要传给调用方。 */
    }
    throw new ApiError(response.status, payload.error)
  }
  const blob = await response.blob()
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = /filename="([^"]+)"/.exec(disposition)
  return {
    blob,
    truncated: response.headers.get('X-Export-Truncated') === 'true',
    filename: match ? match[1] : 'tasks.csv',
  }
}

/** 单账号诊断（运行页行抽屉用）。跨校与跨用户 404。 */
export function getAccountDiagnostics(id: string): Promise<AccountDiagnostics> {
  return apiRequest<AccountDiagnostics>(TASK_PATHS.accountDiagnostics(id))
}
