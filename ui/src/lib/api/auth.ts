import type { components } from './schema'
import { apiRequest, setCsrfToken } from './client'

/**
 * Auth stubs. Every type comes from `schema.d.ts`, which is generated from
 * `server/contracts/openapi.yaml`. Do not redeclare contract shapes here.
 */

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
