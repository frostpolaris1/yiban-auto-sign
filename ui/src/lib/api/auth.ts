import { apiRequest } from './client'

export type Role = 'master' | 'admin' | 'user'

export type SessionUser = {
  id: string
  email: string
  role: Role
}

export type LoginRequest = {
  email: string
  password: string
}

export type ResetRequest = {
  token: string
  password: string
}

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
