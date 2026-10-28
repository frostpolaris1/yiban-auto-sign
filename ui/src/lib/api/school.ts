/**
 * **功能**
 * 学校域 API：类型别名、路径表、列表、新建、编辑、管理员绑定。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写。
 * 列表与管理员列表返回页对象，**不是裸数组**。
 * 全量拉取按 `next_cursor` 循环，单页上限 200，封顶 10 页。
 * 学校删除没有端点，故本文件不提供删除函数。
 */

import type { components, operations } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type School = Schemas['School']
export type SchoolStatus = Schemas['SchoolStatus']
export type SchoolCreate = Schemas['SchoolCreate']
export type SchoolUpdate = Schemas['SchoolUpdate']
export type SchoolPage = Schemas['SchoolPage']
export type SchoolAdmin = Schemas['SchoolAdmin']
export type SchoolAdminPage = Schemas['SchoolAdminPage']
export type SchoolAdminCreate = Schemas['SchoolAdminCreate']

/** 契约 Limit 参数上限。 */
export const SCHOOL_PAGE_LIMIT = 200
/** 防止失控的页数上限。超过时保留已取到的部分。 */
export const SCHOOL_MAX_PAGES = 10

export const SCHOOL_PATHS = {
  list: '/api/schools',
  detail: (id: string) => `/api/schools/${encodeURIComponent(id)}`,
  admins: (id: string) => `/api/schools/${encodeURIComponent(id)}/admins`,
  adminMember: (id: string, uid: string) =>
    `/api/schools/${encodeURIComponent(id)}/admins/${encodeURIComponent(uid)}`,
} as const

/** 查询串视图。字段直接取契约生成的参数，不另写一份。 */
export type SchoolQuery = NonNullable<operations['SchoolList']['parameters']['query']>
export type SchoolAdminQuery = NonNullable<operations['SchoolAdminList']['parameters']['query']>

type CursorQuery = { cursor?: string; limit?: number }

/** 游标与页大小是两类列表唯一的查询参数。空值不出现。 */
function cursorSearch(query: CursorQuery): string {
  const params = new URLSearchParams()
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return search ? `?${search}` : ''
}

export function listSchools(query: SchoolQuery = {}): Promise<SchoolPage> {
  return apiRequest<SchoolPage>(`${SCHOOL_PATHS.list}${cursorSearch(query)}`)
}

export function createSchool(body: SchoolCreate): Promise<School> {
  return apiRequest<School>(SCHOOL_PATHS.list, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** PATCH 只带变更字段。code 创建后不可改，故不在请求体里。 */
export function updateSchool(id: string, body: SchoolUpdate): Promise<School> {
  return apiRequest<School>(SCHOOL_PATHS.detail(id), {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function listSchoolAdmins(
  id: string,
  query: SchoolAdminQuery = {},
): Promise<SchoolAdminPage> {
  return apiRequest<SchoolAdminPage>(`${SCHOOL_PATHS.admins(id)}${cursorSearch(query)}`)
}

export function addSchoolAdmin(id: string, body: SchoolAdminCreate): Promise<SchoolAdmin> {
  return apiRequest<SchoolAdmin>(SCHOOL_PATHS.admins(id), {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 204 解除绑定。成功后无响应体。 */
export function removeSchoolAdmin(id: string, uid: string): Promise<void> {
  return apiRequest<void>(SCHOOL_PATHS.adminMember(id, uid), { method: 'DELETE' })
}

/**
 * 逐页取全。每次请求带 `limit=200`。
 * 页数到 `SCHOOL_MAX_PAGES` 仍无结尾时停止，返回已取到的部分。
 */
async function collectPages<T>(
  fetchPage: (cursor: string | undefined) => Promise<{ items: T[]; next_cursor: string }>,
): Promise<T[]> {
  const items: T[] = []
  let cursor: string | undefined
  for (let page = 0; page < SCHOOL_MAX_PAGES; page += 1) {
    const data = await fetchPage(cursor)
    items.push(...data.items)
    if (!data.next_cursor) break
    cursor = data.next_cursor
  }
  return items
}

export function listAllSchools(): Promise<School[]> {
  return collectPages((cursor) => listSchools({ cursor, limit: SCHOOL_PAGE_LIMIT }))
}

/** 学校管理员模态打开即取全，供完整列表展示。 */
export function listAllSchoolAdmins(id: string): Promise<SchoolAdmin[]> {
  return collectPages((cursor) => listSchoolAdmins(id, { cursor, limit: SCHOOL_PAGE_LIMIT }))
}
