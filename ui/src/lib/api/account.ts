import type { components } from './schema'
import { apiRequest } from './client'

/**
 * Account stubs. Contract shapes come from the generated `schema.d.ts`.
 * `AccountQuery` is the query-string view; its `status` uses the contract enum.
 */

type Schemas = components['schemas']

export type AccountStatus = Schemas['AccountStatus']
export type Account = Schemas['Account']
export type AccountPage = Schemas['AccountPage']

export type AccountQuery = {
  school_id?: string
  status?: AccountStatus
  q?: string
  cursor?: string
}

export const ACCOUNT_PATHS = {
  list: '/api/accounts',
  create: '/api/accounts',
  detail: (id: string) => `/api/accounts/${id}`,
  review: (id: string) => `/api/accounts/${id}/review`,
  pause: (id: string) => `/api/accounts/${id}/pause`,
} as const

export function listAccounts(query: AccountQuery = {}): Promise<AccountPage> {
  const params = new URLSearchParams()
  if (query.school_id) params.set('school_id', query.school_id)
  if (query.status) params.set('status', query.status)
  if (query.q) params.set('q', query.q)
  if (query.cursor) params.set('cursor', query.cursor)
  const search = params.toString()
  return apiRequest<AccountPage>(search ? `${ACCOUNT_PATHS.list}?${search}` : ACCOUNT_PATHS.list)
}
