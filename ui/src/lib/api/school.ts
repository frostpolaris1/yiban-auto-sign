import type { components } from './schema'
import { apiRequest } from './client'

/**
 * School stubs. Contract shapes come from the generated `schema.d.ts`.
 * The list endpoint returns a page object, not a bare array.
 */

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
