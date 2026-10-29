/**
 * **功能**
 * 密码找回两页的纯逻辑：令牌形状、请求体与错误文案。
 *
 * **归属**
 * `features/auth` 域逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 请求体只含契约字段：`ForgotPasswordRequest` 的 `email`，
 * `ResetPasswordRequest` 的 `token` 与 `new_password`。
 * 令牌形状来自服务端唯一生成点 `server/internal/core/auth/auth.go` 的 `NewToken`：
 * 32 字节随机数经 base64url 无填充编码，固定 43 个字符。
 * 非信封错误不回显底层文本：一律改为固定的网络文案。
 */

import type { ForgotPasswordRequest, ResetRequest } from '../../lib/api/auth'
import { ApiError, describeApiError } from '../../lib/api/client'

/**
 * 43 个 base64url 字符。无填充，故不含 `=`。
 * 口径：`NewToken` 的 32 字节经 base64url 无填充编码，得 43 个字符。
 */
const RESET_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/

/** 令牌是否具备可提交的形状。缺省、空串、长度不符、字符越界都不合格。 */
export function isValidResetToken(value: string | null | undefined): value is string {
  return typeof value === 'string' && RESET_TOKEN_PATTERN.test(value)
}

/** 只挑契约字段。草稿里的其他键（例如确认密码）不外泄。 */
export function buildForgotBody(
  draft: { email: string } & Record<string, unknown>,
): ForgotPasswordRequest {
  return { email: draft.email }
}

/** 只挑契约字段。确认密码只用于前端比对，不进请求体。 */
export function buildResetBody(
  draft: { token: string; new_password: string } & Record<string, unknown>,
): ResetRequest {
  return { token: draft.token, new_password: draft.new_password }
}

/** 网络层失败的固定文案。断网时 fetch 抛出 TypeError，没有信封可展示。 */
export const NETWORK_ERROR_MESSAGE = '网络异常，请稍后重试'

/** 错误文案：信封错误照实展示 `code · message`，其余用固定网络文案。 */
export function passwordErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return describeApiError(error)
  return NETWORK_ERROR_MESSAGE
}
