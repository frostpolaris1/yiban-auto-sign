import type { components } from './schema'
import { apiRequest } from './client'

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

export function login(body: LoginRequest): Promise<SessionUser> {
  return apiRequest<SessionUser>(AUTH_PATHS.login, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function logout(): Promise<void> {
  return apiRequest<void>(AUTH_PATHS.logout, { method: 'POST' })
}

export function fetchMe(): Promise<SessionUser> {
  return apiRequest<SessionUser>(AUTH_PATHS.me)
}
