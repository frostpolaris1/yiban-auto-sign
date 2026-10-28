/**
 * **功能**
 * 认证域：登录、登出、取当前会话。
 *
 * **归属**
 * `lib/api` 工具层。
 *
 * **口径**
 * 类型来自 `schema.d.ts`，源是 `server/contracts/openapi.yaml`。
 * **不在此重写契约形状**。
 * 登录与取会话后写入 CSRF 令牌。登出成功后清空。
 */

import type { components } from './schema'
import { apiRequest, setCsrfToken } from './client'

type Schemas = components['schemas']

export type Role = Schemas['UserRole']
export type UserStatus = Schemas['UserStatus']
export type SessionUser = Schemas['SessionUser']
export type LoginRequest = Schemas['LoginRequest']
export type ResetRequest = Schemas['ResetPasswordRequest']

export const AUTH_PATHS = {
  login: '/api/auth/login',
  logout: '/api/auth/logout',
  me: '/api/auth/me',
  forgotPassword: '/api/auth/forgot-password',
  resetPassword: '/api/auth/reset-password',
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
 * 401 分类：登录接口的 401 是凭据错误；`me` 的 401 是未登录常态。
 * 其余端点的 401 才表示会话过期。
 */
export function isSessionExpiry(path: string): boolean {
  return path !== AUTH_PATHS.login && path !== AUTH_PATHS.me
}
