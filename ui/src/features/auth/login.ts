/**
 * **功能**
 * 登录表单的纯逻辑：请求体构造与错误文案。
 *
 * **归属**
 * `features/auth` 域逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 请求体只含契约 `LoginRequest` 的两个字段。
 * 401 是凭据错误，固定文案；其余错误按信封如实展示。
 */

import type { LoginRequest } from '../../lib/api/auth'
import { ApiError, describeApiError } from '../../lib/api/client'

export type LoginDraft = {
  email: string
  password: string
}

/** 只挑契约字段。草稿里的其他键（例如以后误加的选项）不外泄。 */
export function buildLoginBody(draft: LoginDraft & Record<string, unknown>): LoginRequest {
  return { email: draft.email, password: draft.password }
}

export function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) return '邮箱或密码错误'
  return describeApiError(error)
}

/** 路由比较基准。只用于路径规范化，不参与实际导航。 */
const PATH_BASE = 'http://ui.invalid'

/**
 * 目标是否落回登录页。
 * 路由器匹配前做 URI 解码，且大小写不敏感、去尾斜杠：`/LOGIN`、`/login/`、
 * `/login/.`、`/%6Cogin` 都会命中 `/login`。故先解码再按 URL 规范化
 * （点段、大小写、尾斜杠），最后比较 pathname。
 */
export function isLoginPath(target: string): boolean {
  let pathname: string
  try {
    pathname = decodeURI(new URL(target, PATH_BASE).pathname)
  } catch {
    return false
  }
  const normalized = pathname.toLowerCase().replace(/\/+$/, '')
  return normalized === '/login'
}
