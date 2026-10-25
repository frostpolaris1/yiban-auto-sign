/**
 * Shared HTTP client for the Go host API.
 *
 * Contract: the host returns `{ error: { code, message, details, trace_id } }`
 * for failures. No call site exists in this batch; P1 data pages will use it.
 */

export type ApiErrorPayload = {
  code: string
  message: string
  details?: unknown
  trace_id?: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details?: unknown
  readonly traceId?: string

  constructor(status: number, payload: ApiErrorPayload) {
    super(payload.message)
    this.name = 'ApiError'
    this.status = status
    this.code = payload.code
    this.details = payload.details
    this.traceId = payload.trace_id
  }
}

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/**
 * The CSRF token from GET /api/auth/me. The contract requires it on every
 * state-changing request that carries a session, and deriving it needs the
 * session token, which only the server holds.
 */
let csrfToken: string | null = null

export function setCsrfToken(token: string | null): void {
  csrfToken = token
}

export function getCsrfToken(): string | null {
  return csrfToken
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  const response = await fetch(path, {
    credentials: 'include',
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(csrfToken && UNSAFE_METHODS.has(method) ? { 'X-CSRF-Token': csrfToken } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let payload: ApiErrorPayload = {
      code: 'internal_error',
      message: `请求失败（HTTP ${response.status}）`,
    }
    try {
      const body = (await response.json()) as { error?: ApiErrorPayload }
      if (body.error) payload = body.error
    } catch {
      /* Keep the fallback payload when the body is not JSON. */
    }
    throw new ApiError(response.status, payload)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}
