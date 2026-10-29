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
 * 改密是登录态端点：401 表示当前口令错，不表示会话过期。
 */

import type { components } from './schema'
import { apiRequest, setCsrfToken } from './client'

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
 * 登录态自助改密。成功 204，服务端清 `must_change_password`。
 * 401 表示当前口令错；409 表示新旧口令相同。
 * 成功撤销本人其余会话，当前会话保持登录。
 */
export async function changePassword(body: ChangePasswordRequest): Promise<void> {
  await apiRequest<void>(AUTH_PATHS.changePassword, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/**
 * 公开认证端点。契约标 `security: []`：它们的 401 与登录态无关。
 * 忘记密码只有 400/413；重置密码的 401 只表示令牌无效、已用或过期。
 */
const PUBLIC_AUTH_PATHS = new Set<string>([AUTH_PATHS.forgotPassword, AUTH_PATHS.resetPassword])

/**
 * 401 表示输入凭据错误，而不是会话失效。
 * 登录接口的 401 是邮箱或口令错；改密接口的 401 是当前口令错。
 * 这两条都不许触发「会话已过期」处置。
 */
const CREDENTIAL_AUTH_PATHS = new Set<string>([AUTH_PATHS.login, AUTH_PATHS.changePassword])

/**
 * 401 分类：登录与改密的 401 是凭据错误；`me` 的 401 是未登录常态；
 * 密码找回的 401 是令牌问题。
 * 其余端点的 401 才表示会话过期。
 */
export function isSessionExpiry(path: string): boolean {
  return (
    path !== AUTH_PATHS.me && !CREDENTIAL_AUTH_PATHS.has(path) && !PUBLIC_AUTH_PATHS.has(path)
  )
}
