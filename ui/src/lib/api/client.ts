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
  readonly retryAfterSec?: number

  constructor(status: number, payload: ApiErrorPayload, retryAfterSec?: number) {
    super(payload.message)
    this.name = 'ApiError'
    this.status = status
    this.code = payload.code
    this.details = payload.details
    this.traceId = payload.trace_id
    this.retryAfterSec = retryAfterSec
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

const HAS_CJK_RE = /[\u4e00-\u9fff]/

function fallbackChineseByStatus(error: ApiError): string {
  if (error.status === 400) return '请求参数不符合要求，请核对后重试'
  if (error.status === 401) return '登录已过期，请重新登录后再试'
  if (error.status === 403) return '权限不足，无法执行此操作'
  if (error.status === 404) return '目标记录不存在或已删除'
  if (error.status === 409) return '操作与当前状态冲突，请刷新后重试'
  if (error.status === 413) return '提交内容超出大小限制，请精简后重试'
  if (error.status === 429) {
    return error.retryAfterSec
      ? `请求过于频繁，请在 ${error.retryAfterSec} 秒后重试`
      : '请求过于频繁，请稍后重试'
  }
  if (error.status >= 500) return '服务暂不可用，请稍后重试'
  return '请求失败，请稍后重试'
}

/**
 * 错误信封的用户展示文本：不暴露内部 `code ·` 前缀。
 * 若服务端消息已含可读中文则直接返回；纯英文或机器错误按 HTTP 状态收口为中文兜底。
 */
export function describeApiError(error: unknown): string {
  if (error instanceof ApiError) {
    const trimmed = error.message.trim()
    if (trimmed !== '' && HAS_CJK_RE.test(trimmed)) return trimmed
    return fallbackChineseByStatus(error)
  }
  if (error instanceof Error) {
    const trimmed = error.message.trim()
    if (trimmed !== '' && HAS_CJK_RE.test(trimmed)) return trimmed
    return '请求异常，请稍后重试'
  }
  return String(error)
}

/** 409 是业务结论（并发、最后一名 master、名下有账号），不是传输故障。 */
export function isConflictError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

/**
 * 危险写二次口令的失败文案。400 缺口令、403 口令错、429 连续失败
 * 三条各自可读；其余状态仍回统一信封，便于定位非口令类失败。
 */
export function describeReauthError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '缺少本站登录口令，或填写内容不合法。'
    if (error.status === 403) return '口令不正确，或当前状态不允许这个操作。'
    // 429 有两种来源：二次口令连续失败，或业务容量拒绝。只有前者换成口令文案。
    if (error.status === 429 && error.code === 'rate_limited') return '尝试过多，请稍后再试。'
    if (error.status === 502) return '平台暂时不可用，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
  }
  return describeApiError(error)
}

function parseRetryAfterSeconds(response: Response): number | undefined {
  const raw = response.headers.get('Retry-After')?.trim()
  if (!raw || !/^\d+$/.test(raw)) return undefined
  const seconds = Number.parseInt(raw, 10)
  return Number.isFinite(seconds) && seconds > 0 ? seconds : undefined
}

/** 把失败响应翻成 `ApiError`：先读信封与 Retry-After，再交 401 回调。 */
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
  const retryAfterSec = parseRetryAfterSeconds(response)
  if (response.status === 401) unauthorizedHandler?.(path)
  throw new ApiError(response.status, payload, retryAfterSec)
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
