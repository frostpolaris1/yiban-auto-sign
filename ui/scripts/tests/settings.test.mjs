import assert from 'node:assert/strict'
import test from 'node:test'

import { isSessionExpiry } from '../../src/lib/api/auth.ts'
import { ApiError } from '../../src/lib/api/client.ts'
import {
  NETWORK_ERROR_MESSAGE,
  buildChangePasswordBody,
  changePasswordErrorMessage,
  changePasswordFieldError,
  userStatusLabel,
  validateChangePassword,
} from '../../src/features/settings/change-password.ts'

/**
 * 账号设置页的纯逻辑不变式。
 *
 * 就地校验：三个字段必填；新口令不得等于当前口令；两次输入必须一致。
 * 请求体：只有契约的两个字段，确认字段不外泄。
 * 错误落点：401 挂当前密码，409 挂新密码，其余走通栏。
 * 401 分类：改密的 401 是旧口令错，不是会话过期。
 */

const CURRENT = 'not-a-real-password'
const NEXT = 'not-a-real-new-password'

test('a complete draft with distinct entries has no local error', () => {
  assert.deepEqual(
    validateChangePassword({ current: CURRENT, next: NEXT, confirm: NEXT }),
    {},
  )
})

test('every empty field gets its own message', () => {
  assert.deepEqual(validateChangePassword({ current: '', next: '', confirm: '' }), {
    current: '请输入当前密码',
    next: '请输入新密码',
    confirm: '请再次输入新密码',
  })
})

test('an empty new password never reports the reused-password error', () => {
  // 两个空串相等，但空是非空判据先命中：文案必须是「请输入新密码」。
  const errors = validateChangePassword({ current: '', next: '', confirm: '' })
  assert.notEqual(errors.next, '新密码不能与当前密码相同')
})

test('a new password equal to the current one is rejected before any request', () => {
  const errors = validateChangePassword({ current: CURRENT, next: CURRENT, confirm: CURRENT })
  assert.equal(errors.next, '新密码不能与当前密码相同')
  assert.equal(errors.current, undefined)
  assert.equal(errors.confirm, undefined)
})

test('a mismatched confirmation is rejected before any request', () => {
  const errors = validateChangePassword({
    current: CURRENT,
    next: NEXT,
    confirm: `${NEXT}-other`,
  })
  assert.equal(errors.confirm, '两次输入的新密码不一致')
  assert.equal(errors.next, undefined)
})

test('the change-password body carries exactly the contract fields', () => {
  const body = buildChangePasswordBody({
    current: CURRENT,
    next: NEXT,
    confirm: NEXT,
    email: 'a@example.com',
  })
  assert.deepEqual(Object.keys(body).sort(), ['current_password', 'new_password'])
  assert.deepEqual(body, { current_password: CURRENT, new_password: NEXT })
})

test('a wrong current password becomes a field error on the current field', () => {
  const fieldError = changePasswordFieldError(
    new ApiError(401, { code: 'unauthorized', message: 'invalid email or password' }),
  )
  assert.deepEqual(fieldError, { field: 'current', message: '当前密码不正确' })
})

test('a reused password conflict becomes a field error on the new field', () => {
  const fieldError = changePasswordFieldError(
    new ApiError(409, {
      code: 'conflict',
      message: 'new password must differ from the current password',
    }),
  )
  assert.deepEqual(fieldError, { field: 'next', message: '新密码不能与当前密码相同' })
})

test('every other failure has no field target and keeps the envelope', () => {
  const validation = new ApiError(400, {
    code: 'validation_failed',
    message: 'new_password is required',
  })
  assert.equal(changePasswordFieldError(validation), null)
  assert.equal(changePasswordErrorMessage(validation), 'validation_failed · new_password is required')

  const oversized = new ApiError(413, {
    code: 'validation_failed',
    message: 'request body too large',
  })
  assert.equal(changePasswordFieldError(oversized), null)
  assert.equal(changePasswordErrorMessage(oversized), 'validation_failed · request body too large')
})

test('a transport failure becomes one fixed network message', () => {
  assert.equal(changePasswordFieldError(new TypeError('Failed to fetch')), null)
  assert.equal(changePasswordErrorMessage(new TypeError('Failed to fetch')), NETWORK_ERROR_MESSAGE)
  assert.equal(NETWORK_ERROR_MESSAGE, '网络异常，请稍后重试')
})

test('the user status uses the same two words as the school status', () => {
  assert.equal(userStatusLabel('active'), '正常')
  assert.equal(userStatusLabel('disabled'), '停用')
})

test('a change-password 401 is a credential error, not a session expiry', () => {
  // 旧口令错不能把已登录用户弹回登录页。
  assert.equal(isSessionExpiry('/api/auth/change-password'), false)
  assert.equal(isSessionExpiry('/api/auth/login'), false)
  assert.equal(isSessionExpiry('/api/auth/me'), false)
  assert.equal(isSessionExpiry('/api/accounts'), true)
})
