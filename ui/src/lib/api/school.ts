import { apiRequest } from './client'

export type School = {
  id: string
  name: string
  code: string
  status: 'active' | 'disabled'
  timezone: string
}

export const SCHOOL_PATHS = {
  list: '/api/schools',
  detail: (id: string) => `/api/schools/${id}`,
  admins: (id: string) => `/api/schools/${id}/admins`,
} as const

export function listSchools(): Promise<School[]> {
  return apiRequest<School[]>(SCHOOL_PATHS.list)
}
