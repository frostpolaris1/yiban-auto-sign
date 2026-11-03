/**
 * **功能**
 * 宿主 API 的共享 HTTP 客户端。
 * 统一处理 cookie、CSRF 头、错误信封、204 与 GET 条件请求。
 *
 * **归属**
 * `lib/api` 工具层：各域桩都经此发请求。
 *
 * **口径**
 * 失败信封固定 `{ error: { code, message, details, trace_id } }`。
 * 请求一律带 cookie。写方法另带 `X-CSRF-Token`。
 * 401 另交注册的全局回调处置；分类规则在 `auth.ts`。
 * GET 有缓存时带 `If-None-Match`；304 复用缓存体，200 带 ETag 时更新。
 * 缓存键是完整路径（含查询串）；`init.headers` 的显式值优先。
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

/** 401 回调。回调只收到请求路径；要不要处置由注册方判。 */
export type UnauthorizedHandler = (path: string) => void

let unauthorizedHandler: UnauthorizedHandler | null = null

/**
 * 注册全局 401 回调。装配层注册，`lib` 不反向依赖 `app`。
 * 传 `null` 注销。
 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

/** 错误信封的统一展示文本：`code · message`。非 ApiError 退回自身消息。 */
export function describeApiError(error: unknown): string {
  if (error instanceof ApiError) return `${error.code} · ${error.message}`
  if (error instanceof Error) return error.message
  return String(error)
}

/** 把失败响应翻成 `ApiError`：先读信封，再交 401 回调。 */
async function raiseApiError(response: Response, path: string): Promise<never> {
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
  if (response.status === 401) unauthorizedHandler?.(path)
  throw new ApiError(response.status, payload)
}

/** 条件请求缓存的上限。列表每页一个键，超出按插入序删最旧。 */
const CONDITIONAL_CACHE_LIMIT = 100

type CachedBody = { etag: string; body: unknown }

/** 键是完整路径（含查询串）；值是该表示的 ETag 与解析后的响应体。 */
const conditionalCache = new Map<string, CachedBody>()

function rememberBody(path: string, etag: string, body: unknown): void {
  // 先删再插：更新过的键排到队尾，淘汰顺序与「最久未更新」一致。
  conditionalCache.delete(path)
  conditionalCache.set(path, { etag, body })
  if (conditionalCache.size > CONDITIONAL_CACHE_LIMIT) {
    const oldest = conditionalCache.keys().next().value
    if (oldest !== undefined) conditionalCache.delete(oldest)
  }
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  const cached = conditionalCache.get(path)
  const explicitHeaders = new Headers(init?.headers)
  // 写请求不带 If-None-Match；显式头永远压过缓存。
  const conditional = method === 'GET' && !explicitHeaders.has('If-None-Match')
  const response = await fetch(path, {
    credentials: 'include',
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(csrfToken && UNSAFE_METHODS.has(method) ? { 'X-CSRF-Token': csrfToken } : {}),
      ...(conditional && cached ? { 'If-None-Match': cached.etag } : {}),
      ...init?.headers,
    },
  })

  // 304 只省响应体：有缓存就交回同一对象，让结构共享保持引用稳定。
  // 显式 If-None-Match 的 304 只证明调用方那一值命中，不能复用本地缓存体。
  if (response.status === 304) {
    if (cached && conditional) {
      // 命中也要刷新插入序：轮询键不能被误当成最旧而先淘汰。
      rememberBody(path, cached.etag, cached.body)
      return cached.body as T
    }
    return raiseApiError(response, path)
  }

  if (!response.ok) return raiseApiError(response, path)

  // 204 无响应体。此时解析 JSON 会抛错，故直接返回。
  if (response.status === 204) {
    if (method === 'GET') conditionalCache.delete(path)
    return undefined as T
  }
  const body = (await response.json()) as T
  if (method === 'GET') {
    const etag = response.headers.get('ETag')
    if (etag) rememberBody(path, etag, body)
    else conditionalCache.delete(path)
  }
  return body
}

/**
 * 上传请求：请求体是 `FormData`。
 *
 * 此路径**不**设 `Content-Type`。浏览器要自己写 multipart 边界；
 * 手写 JSON 类型会让服务端解析失败。其余口径与 `apiRequest` 相同。
 */
export async function apiUpload<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    body: form,
    headers: {
      Accept: 'application/json',
      ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
    },
  })

  if (!response.ok) return raiseApiError(response, path)
  return (await response.json()) as T
}
