import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  ACTIVE_STATUS_LABEL,
  DISABLED_STATUS_LABEL,
  roleLabel,
} from '../../src/lib/identity.ts'
import {
  USER_ROLE_OPTIONS,
  USER_STATUS_OPTIONS,
  userStatusLabel,
} from '../../src/features/user/display.ts'

/**
 * 用户管理页的状态文案不变式。
 *
 * 一词一义：`active` 与 `disabled` 的文案只在 `lib/identity` 定义一次。
 * 用户页的表是「状态 → 共用词」的引用表，不是第三份字面量。
 * 角色文案同理，取 `roleLabel`。
 */

test('the user status words are the shared identity words', () => {
  assert.equal(userStatusLabel('active'), ACTIVE_STATUS_LABEL)
  assert.equal(userStatusLabel('disabled'), DISABLED_STATUS_LABEL)
  // 选项表与行内徽章同一来源：两处文案不得漂移。
  assert.deepEqual(
    USER_STATUS_OPTIONS.map((option) => option.label),
    ['active', 'disabled'].map((value) => userStatusLabel(value)),
  )
})

test('the user role labels come from roleLabel', () => {
  assert.deepEqual(
    USER_ROLE_OPTIONS.map((option) => [option.value, option.label]),
    ['master', 'admin', 'user'].map((role) => [role, roleLabel(role)]),
  )
})

test('the user feature never restates the status words', () => {
  // 值相等挡不住「两处各写一份字面量」。扫描源文件，字面量只能待在 identity。
  const source = readFileSync(
    new URL('../../src/features/user/display.ts', import.meta.url),
    'utf8',
  )
  for (const word of [ACTIVE_STATUS_LABEL, DISABLED_STATUS_LABEL]) {
    assert.ok(
      !source.includes(`'${word}'`),
      `${word} must not be written in features/user/display.ts`,
    )
  }
})
