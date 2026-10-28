/**
 * **功能**
 * 学校域 API：类型别名、路径表、列表查询、全量拉取。
 *
 * **归属**
 * `lib/api` 工具层。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写。
 * 列表返回页对象，**不是裸数组**。
 * 过滤用全量拉取按 next_cursor 循环，单页上限 200，封顶 10 页。
 */

import type { components, operations } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type School = Schemas['School']
export type SchoolPage = Schemas['SchoolPage']

/** 契约 Limit 参数上限。 */
export const SCHOOL_PAGE_LIMIT = 200
/** 防止失控的页数上限。超过时保留已取到的部分。 */
export const SCHOOL_MAX_PAGES = 10

export const SCHOOL_PATHS = {
  list: '/api/schools',
  detail: (id: string) => `/api/schools/${encodeURIComponent(id)}`,
  admins: (id: string) => `/api/schools/${encodeURIComponent(id)}/admins`,
} as const

/** 查询串视图。字段直接取契约生成的 `SchoolList` 参数，不另写一份。 */
export type SchoolQuery = NonNullable<operations['SchoolList']['parameters']['query']>

export function listSchools(query: SchoolQuery = {}): Promise<SchoolPage> {
  const params = new URLSearchParams()
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return apiRequest<SchoolPage>(search ? `${SCHOOL_PATHS.list}?${search}` : SCHOOL_PATHS.list)
}

/**
 * 取全部学校。每次请求带 `limit=200`。
 * 页数到 `SCHOOL_MAX_PAGES` 仍无结尾时停止，返回已取到的部分。
 */
export async function listAllSchools(): Promise<School[]> {
  const items: School[] = []
  let cursor: string | undefined
  for (let page = 0; page < SCHOOL_MAX_PAGES; page += 1) {
    const data = await listSchools({ cursor, limit: SCHOOL_PAGE_LIMIT })
    items.push(...data.items)
    if (!data.next_cursor) break
    cursor = data.next_cursor
  }
  return items
}
