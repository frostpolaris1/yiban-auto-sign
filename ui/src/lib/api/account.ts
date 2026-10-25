import { apiRequest } from './client'

export type AccountStatus = 'pending' | 'active' | 'paused' | 'deleted'

export type Account = {
  id: string
  school_id: string
  phone_masked: string
  status: AccountStatus
  created_at: string
}

export type AccountPage = {
  items: Account[]
  next_cursor: string | null
}

export type AccountQuery = {
  school_id?: string
  status?: AccountStatus
  q?: string
  cursor?: string | null
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
