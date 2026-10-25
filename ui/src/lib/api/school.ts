/**
 * **功能**
 * 学校域桩：类型别名、路径表、列表查询。
 *
 * **归属**
 * `lib/api` 工具层。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写。
 * 列表返回页对象，**不是裸数组**。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type School = Schemas['School']
export type SchoolPage = Schemas['SchoolPage']

export const SCHOOL_PATHS = {
  list: '/api/schools',
  detail: (id: string) => `/api/schools/${id}`,
  admins: (id: string) => `/api/schools/${id}/admins`,
} as const

export function listSchools(): Promise<SchoolPage> {
  return apiRequest<SchoolPage>(SCHOOL_PATHS.list)
}
