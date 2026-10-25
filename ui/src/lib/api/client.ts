/**
 * **功能**
 * 宿主 API 的共享 HTTP 客户端。
 * 统一处理 cookie、CSRF 头、错误信封与 204。
 *
 * **归属**
 * `lib/api` 工具层：各域桩都经此发请求。
 *
 * **口径**
 * 失败信封固定 `{ error: { code, message, details, trace_id } }`。
 * 请求一律带 cookie。写方法另带 `X-CSRF-Token`。
 * 本批没有调用点。P1 数据页接入。
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
 * CSRF 令牌来自 `GET /api/auth/me`。
 * 契约要求带会话的写请求都附上它。
 * 推导令牌需要会话令牌，而后者只在服务端。
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
      /* 响应体不是 JSON 时保留兜底错误，状态码仍要传给调用方。 */
    }
    throw new ApiError(response.status, payload)
  }

  // 204 无响应体。此时解析 JSON 会抛错，故直接返回。
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}
