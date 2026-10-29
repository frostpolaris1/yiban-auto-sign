/**
 * **功能**
 * 改密表单的纯逻辑：就地校验、请求体与错误落点。
 *
 * **归属**
 * `features/settings` 域逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 请求体只含契约 `ChangePasswordRequest` 的两个字段；确认字段只在前端比对。
 * 三个字段都必填。口令可以有空格，故空判据是长度为零，不做 trim。
 * 新口令等于当前口令在页面内拦截，不发请求。
 * 409 由服务端下结论，界面用固定文案，不展示后端原文。
 * 400 与其余信封错误照实展示 `code · message`。
 * 401 先由 `lib/api/auth` 探一次 me：me 200 是当前口令错，me 401 是会话失效。
 * 用户状态文案在 `lib/identity`，与角色文案同处。
 */

import type { ChangePasswordRequest } from '../../lib/api/auth'
import { ApiError, describeApiError } from '../../lib/api/client'

export type ChangePasswordDraft = {
  current: string
  next: string
  confirm: string
}

export type ChangePasswordField = 'current' | 'next' | 'confirm'

/** 字段名到就地错误文案。缺失即该字段通过。 */
export type ChangePasswordErrors = Partial<Record<ChangePasswordField, string>>

/** 网络层失败的固定文案。断网时 fetch 抛出 TypeError，没有信封可展示。 */
export const NETWORK_ERROR_MESSAGE = '网络异常，请稍后重试'

/** 改密成功的 toast 文案。全仓只此一份。 */
export const PASSWORD_CHANGED_TOAST = '密码已修改'

export const CURRENT_PASSWORD_REQUIRED = '请输入当前密码'
export const NEW_PASSWORD_REQUIRED = '请输入新密码'
export const CONFIRM_PASSWORD_REQUIRED = '请再次输入新密码'
export const PASSWORD_UNCHANGED = '新密码不能与当前密码相同'
export const PASSWORD_MISMATCH = '两次输入的新密码不一致'

/**
 * 就地校验。返回空对象即可以提交。
 * 顺序固定为当前、新、确认：提交失败时焦点取第一个出错字段。
 */
export function validateChangePassword(draft: ChangePasswordDraft): ChangePasswordErrors {
  const errors: ChangePasswordErrors = {}
  if (draft.current === '') errors.current = CURRENT_PASSWORD_REQUIRED
  if (draft.next === '') {
    errors.next = NEW_PASSWORD_REQUIRED
  } else if (draft.next === draft.current) {
    errors.next = PASSWORD_UNCHANGED
  }
  if (draft.confirm === '') {
    errors.confirm = CONFIRM_PASSWORD_REQUIRED
  } else if (draft.next !== '' && draft.confirm !== draft.next) {
    errors.confirm = PASSWORD_MISMATCH
  }
  return errors
}

/** 只挑契约字段。确认字段与其他键都不外泄。 */
export function buildChangePasswordBody(
  draft: ChangePasswordDraft & Record<string, unknown>,
): ChangePasswordRequest {
  return { current_password: draft.current, new_password: draft.next }
}

/**
 * 服务端错误的字段落点。
 * 401 挂当前密码（调用前已由 me 探针排除会话失效），409 挂新密码；
 * 其余错误返回 null，由通栏提示承载。
 */
export function changePasswordFieldError(
  error: unknown,
): { field: 'current' | 'next'; message: string } | null {
  if (!(error instanceof ApiError)) return null
  if (error.status === 401) return { field: 'current', message: '当前密码不正确' }
  if (error.status === 409) return { field: 'next', message: PASSWORD_UNCHANGED }
  return null
}

/** 通栏错误文案：信封错误照实展示，其余用固定网络文案。 */
export function changePasswordErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return describeApiError(error)
  return NETWORK_ERROR_MESSAGE
}
