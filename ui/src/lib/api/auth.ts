/**
 * **功能**
 * 认证域：登录、登出、取当前会话、密码找回、登录态改密。
 *
 * **归属**
 * `lib/api` 工具层。
 *
 * **口径**
 * 类型来自 `schema.d.ts`，源是 `server/contracts/openapi.yaml`。
 * **不在此重写契约形状**。
 * 登录与取会话后写入 CSRF 令牌。登出成功后清空。
 * 密码找回两端点都是 public：无会话、无 CSRF。
 * 改密是登录态端点：401 有两种来源——当前口令错、会话失效。
 * 客户端用 `GET /api/auth/me` 探针区分；服务端让两者不可区分。
 */

import type { components } from './schema'
import { ApiError, apiRequest, setCsrfToken } from './client'

type Schemas = components['schemas']

export type Role = Schemas['UserRole']
export type UserStatus = Schemas['UserStatus']
export type SessionUser = Schemas['SessionUser']
export type LoginRequest = Schemas['LoginRequest']
export type ForgotPasswordRequest = Schemas['ForgotPasswordRequest']
export type ResetRequest = Schemas['ResetPasswordRequest']
export type ChangePasswordRequest = Schemas['ChangePasswordRequest']

export const AUTH_PATHS = {
  login: '/api/auth/login',
  logout: '/api/auth/logout',
  me: '/api/auth/me',
  forgotPassword: '/api/auth/forgot-password',
  resetPassword: '/api/auth/reset-password',
  changePassword: '/api/auth/change-password',
} as const

export async function login(body: LoginRequest): Promise<SessionUser> {
  const me = await apiRequest<SessionUser>(AUTH_PATHS.login, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  setCsrfToken(me.csrf_token)
  return me
}

export async function logout(): Promise<void> {
  await apiRequest<void>(AUTH_PATHS.logout, { method: 'POST' })
  setCsrfToken(null)
}

export async function fetchMe(): Promise<SessionUser> {
  const me = await apiRequest<SessionUser>(AUTH_PATHS.me)
  setCsrfToken(me.csrf_token)
  return me
}

/**
 * 忘记密码。契约恒 204：邮箱是否存在，响应都一样。
 * 调用方不得据响应推断账号存在性。
 */
export async function forgotPassword(body: ForgotPasswordRequest): Promise<void> {
  await apiRequest<void>(AUTH_PATHS.forgotPassword, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 用一次性令牌替换口令。成功 204；令牌无效、已用或过期是 401。 */
export async function resetPassword(body: ResetRequest): Promise<void> {
  await apiRequest<void>(AUTH_PATHS.resetPassword, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/**
 * 改密 401 且 me 探针也 401：会话确实失效。
 * 调用方据此走会话过期处置，不得显示「当前密码不正确」。
 */
export class SessionExpiredError extends Error {
  constructor() {
    super('session expired')
    this.name = 'SessionExpiredError'
  }
}

/**
 * 登录态自助改密。成功 204，服务端清 `must_change_password`。
 * 401 有两种来源：当前口令错、会话失效。静默探一次 me 区分：
 * me 200 ⇒ 当前口令错，抛出原 401；me 401 ⇒ `SessionExpiredError`。
 * 探针的其他失败原样抛出，不猜。409 表示新旧口令相同。
 * 成功撤销本人其余会话，当前会话保持登录。
 */
export async function changePassword(body: ChangePasswordRequest): Promise<void> {
  try {
    await apiRequest<void>(AUTH_PATHS.changePassword, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) throw error
    // 同一路由的 401 有两种来源：口令校验失败、中间件拒绝。
    // 服务端让两者不可区分，客户端以 me 探针定论。
    let alive: boolean
    try {
      await fetchMe()
      alive = true
    } catch (probe) {
      if (probe instanceof ApiError && probe.status === 401) alive = false
      else throw probe
    }
    if (!alive) throw new SessionExpiredError()
    throw error
  }
}

/**
 * 公开认证端点。契约标 `security: []`：它们的 401 与登录态无关。
 * 忘记密码只有 400/413；重置密码的 401 只表示令牌无效、已用或过期。
 */
const PUBLIC_AUTH_PATHS = new Set<string>([AUTH_PATHS.forgotPassword, AUTH_PATHS.resetPassword])

/**
 * 401 不在路径层直接触发会话过期处置的端点。
 * 登录接口的 401 是邮箱或口令错。
 * 改密接口的 401 可能是当前口令错，也可能是会话失效；
 * 由 `changePassword` 的 me 探针下结论，全局处置不得抢跑。
 */
const CREDENTIAL_AUTH_PATHS = new Set<string>([AUTH_PATHS.login, AUTH_PATHS.changePassword])

/**
 * 401 分类：登录的 401 是凭据错误；改密的 401 待 me 探针定论；
 * `me` 的 401 是未登录常态；密码找回的 401 是令牌问题。
 * 其余端点的 401 才表示会话过期。
 */
export function isSessionExpiry(path: string): boolean {
  return (
    path !== AUTH_PATHS.me && !CREDENTIAL_AUTH_PATHS.has(path) && !PUBLIC_AUTH_PATHS.has(path)
  )
}
