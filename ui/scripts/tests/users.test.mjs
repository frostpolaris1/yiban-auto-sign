import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  ACTIVE_STATUS_LABEL,
  DISABLED_STATUS_LABEL,
  roleLabel,
} from '../../src/lib/identity.ts'
import {
  nextGroupWithMembers,
  USER_GROUP_LABELS,
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

// --- UX-1：URL 状态的解析、序列化与「筛选即回第一页」 -----------------------

import {
  parseUserSearchRecord,
  userSearchRecord,
  withDeleted,
  withFilter,
  withGroup,
} from '../../src/features/user/urlState.ts'

test('the URL state parser keeps only known, valid values', () => {
  const state = parseUserSearchRecord({
    role: 'admin',
    status: 'disabled',
    q: 'mem',
    deleted: '1',
    cursor: 'c2',
  })
  assert.deepEqual(state, { role: 'admin', status: 'disabled', q: 'mem', deleted: true, cursor: 'c2' })
})

test('the URL state parser drops invalid values instead of passing them on', () => {
  const state = parseUserSearchRecord({
    role: 'root',
    status: 'deleted',
    q: '',
    deleted: 'no',
    cursor: '',
    junk: 'x',
  })
  assert.deepEqual(state, {})
})

test('the URL search record omits empty values so equal states share one URL', () => {
  assert.deepEqual(userSearchRecord({}), {})
  assert.deepEqual(
    userSearchRecord({ role: 'user', deleted: true, cursor: 'c9' }),
    { role: 'user', deleted: '1', cursor: 'c9' },
  )
})

test('parse and write round-trip without losing a value', () => {
  const state = { role: 'master', status: 'active', q: 'a', deleted: true, cursor: 'c1' }
  assert.deepEqual(parseUserSearchRecord(userSearchRecord(state)), state)
})

test('changing a filter always drops the cursor', () => {
  const next = withFilter({ role: 'user', q: 'a', cursor: 'c2' }, { status: 'active' })
  assert.equal(next.cursor, undefined)
  assert.equal(next.role, 'user')
  assert.equal(next.status, 'active')
  // 空 q 不产生键：同状态同 URL。
  const cleared = withFilter({ role: 'user', q: 'a' }, { q: undefined })
  assert.equal(cleared.q, undefined)
  assert.deepEqual(userSearchRecord(cleared), { role: 'user' })
})

// --- 分组：URL 状态与互斥 ---------------------------------------------------

test('the URL state parser keeps a known group and lets it win over deleted', () => {
  assert.deepEqual(parseUserSearchRecord({ group: 'pending' }), { group: 'pending' })
  // 手写 URL 同时带 group 与 deleted：分组优先，软删行不参与分组。
  assert.deepEqual(parseUserSearchRecord({ group: 'vacant', deleted: '1' }), { group: 'vacant' })
  assert.deepEqual(parseUserSearchRecord({ group: 'bogus' }), {})
})

test('the URL search record never writes group and deleted together', () => {
  assert.deepEqual(userSearchRecord({ group: 'normal', cursor: 'c1' }), {
    group: 'normal',
    cursor: 'c1',
  })
  assert.deepEqual(userSearchRecord({ group: 'normal', deleted: true }), { group: 'normal' })
  assert.deepEqual(userSearchRecord({ deleted: true }), { deleted: '1' })
})

test('group and deleted are mutually exclusive and both drop the cursor', () => {
  const grouped = withGroup({ role: 'user', deleted: true, cursor: 'c2' }, 'pending')
  assert.equal(grouped.group, 'pending')
  assert.equal(grouped.deleted, undefined)
  assert.equal(grouped.cursor, undefined)
  // 全部标签也退出已删除视图。
  const all = withGroup({ deleted: true }, undefined)
  assert.equal(all.group, undefined)
  assert.equal(all.deleted, undefined)

  const deleted = withDeleted({ role: 'user', group: 'vacant', cursor: 'c3' }, true)
  assert.equal(deleted.deleted, true)
  assert.equal(deleted.group, undefined)
  assert.equal(deleted.cursor, undefined)
  const live = withDeleted({ deleted: true }, false)
  assert.equal(live.deleted, undefined)
})

test('an empty group points at the nearest non-empty sibling', () => {
  assert.deepEqual(
    nextGroupWithMembers('pending', { pending: 0, normal: 3, vacant: 1, deleted: 9 }),
    { group: 'normal', label: '查看正式用户' },
  )
  assert.deepEqual(
    nextGroupWithMembers('normal', { pending: 1, normal: 0, vacant: 2, deleted: 0 }),
    { group: 'vacant', label: '查看空用户' },
  )
  assert.deepEqual(
    nextGroupWithMembers('vacant', { pending: 1, normal: 0, vacant: 0, deleted: 0 }),
    { group: 'pending', label: '查看待处理用户' },
  )
  assert.equal(nextGroupWithMembers('pending', undefined), null)
  assert.equal(
    nextGroupWithMembers('pending', { pending: 0, normal: 0, vacant: 0, deleted: 3 }),
    null,
  )
  assert.deepEqual(Object.keys(USER_GROUP_LABELS), ['pending', 'normal', 'vacant'])
})
