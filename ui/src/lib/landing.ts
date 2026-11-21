/**
 * **功能**
 * 登录后的落地页：按角色选用户端或管理台，并净化回跳地址。
 *
 * **归属**
 * `lib` 工具层：纯函数，无 React、无网络。
 *
 * **口径**
 * `user` 只进 `/app`；`master`/`admin` 只进 `/`。
 * 回跳地址先经 `sanitizeRedirect`；登录/注册页自身不作为落地。
 * 角色与目标面不匹配时，落到该角色的默认页，不把用户送进无权限壳。
 */

import type { Role } from './api/auth'
import { sanitizeRedirect } from './redirect'

function isAuthPath(target: string): boolean {
  let pathname: string
  try {
    pathname = decodeURI(new URL(target, 'http://ui.invalid').pathname)
  } catch {
    return false
  }
  const normalized = pathname.toLowerCase().replace(/\/+$/, '')
  return normalized === '/login' || normalized === '/register'
}

export function landingForRole(role: Role): string {
  return role === 'user' ? '/app' : '/'
}

function isUserSurfacePath(target: string): boolean {
  return (
    target === '/app' ||
    target.startsWith('/app/') ||
    target.startsWith('/app?') ||
    target.startsWith('/app#')
  )
}

export function resolveLanding(role: Role, redirect: string | undefined): string {
  const target = sanitizeRedirect(redirect)
  if (!target || isAuthPath(target)) return landingForRole(role)
  if (role === 'user') {
    return isUserSurfacePath(target) ? target : '/app'
  }
  return isUserSurfacePath(target) ? '/' : target
}
