import assert from 'node:assert/strict'
import test from 'node:test'

import { buildAccountSearch } from '../../src/lib/api/account.ts'
import { listAllSchools } from '../../src/lib/api/school.ts'
import {
  can,
  canChangePause,
  denyReason,
  pauseDenyReason,
  pauseDirection,
} from '../../src/features/accounts/permissions.ts'

/**
 * 账号页纯逻辑的不变式。
 *
 * 查询串：只带非空筛选。空串与 undefined 都不出现在 URL 里。
 * 暂停谓词：暂停只从 active 出发；恢复只认匹配的种类与角色。
 * 学校列表：按 next_cursor 取全，封顶 10 页。
 * 游标栈是共享原语，其单测在 `cursor.test.mjs`。
 */

function queryParams(search) {
  return Object.fromEntries(new URLSearchParams(search).entries())
}

test('buildAccountSearch omits every empty filter', () => {
  assert.equal(buildAccountSearch({}), '')
  assert.equal(
    buildAccountSearch({ school_id: '', status: undefined, q: '', cursor: '' }),
    '',
  )
})

test('buildAccountSearch carries each filter with its contract name', () => {
  assert.deepEqual(queryParams(buildAccountSearch({ status: 'active' })), {
    status: 'active',
  })
  assert.deepEqual(
    queryParams(
      buildAccountSearch({
        school_id: 'school-b',
        status: 'paused_admin',
        q: '13800138000',
        cursor: 'c2',
      }),
    ),
    {
      school_id: 'school-b',
      status: 'paused_admin',
      q: '13800138000',
      cursor: 'c2',
    },
  )
})

const STATUSES = [
  'pending',
  'active',
  'rejected',
  'paused_admin',
  'paused_user',
  'credential_error',
]

const PAUSE_MATRIX = {
  master: {
    pending: false,
    active: true,
    rejected: false,
    paused_admin: true,
    paused_user: false,
    credential_error: false,
  },
  admin: {
    pending: false,
    active: true,
    rejected: false,
    paused_admin: true,
    paused_user: false,
    credential_error: false,
  },
  user: {
    pending: false,
    active: true,
    rejected: false,
    paused_admin: false,
    paused_user: true,
    credential_error: false,
  },
}

test('pause and resume follow the state and role matrix', () => {
  for (const [role, row] of Object.entries(PAUSE_MATRIX)) {
    for (const status of STATUSES) {
      assert.equal(canChangePause(role, status), row[status], `${role} ${status}`)
    }
  }
  for (const status of STATUSES) {
    assert.equal(canChangePause(null, status), false, `no role ${status}`)
    assert.equal(canChangePause(undefined, status), false, `no role ${status}`)
  }
})

test('the button says pause unless the account is already paused', () => {
  for (const status of STATUSES) {
    const want = status === 'paused_admin' || status === 'paused_user' ? 'resume' : 'pause'
    assert.equal(pauseDirection(status), want, status)
  }
})

test('pause and resume denials name the failed condition', () => {
  assert.match(pauseDenyReason('master', 'pending'), /仅正常状态可暂停/)
  assert.match(pauseDenyReason('master', 'rejected'), /仅正常状态可暂停/)
  assert.match(pauseDenyReason('master', 'credential_error'), /仅正常状态可暂停/)
  assert.match(pauseDenyReason('user', 'paused_admin'), /对应角色/)
  assert.match(pauseDenyReason('admin', 'paused_user'), /对应角色/)
  assert.match(pauseDenyReason(null, 'active'), /会话/)
  assert.match(pauseDenyReason(undefined, 'paused_admin'), /会话/)

  assert.equal(pauseDenyReason('master', 'active'), null)
  assert.equal(pauseDenyReason('master', 'paused_admin'), null)
  assert.equal(pauseDenyReason('admin', 'paused_admin'), null)
  assert.equal(pauseDenyReason('user', 'active'), null)
  assert.equal(pauseDenyReason('user', 'paused_user'), null)
})

test('every role may sign but only admins verify and review', () => {
  for (const role of ['master', 'admin', 'user']) {
    assert.equal(can(role, 'sign'), true, `${role} sign`)
  }
  for (const role of ['master', 'admin']) {
    assert.equal(can(role, 'verify'), true, `${role} verify`)
    assert.equal(can(role, 'review'), true, `${role} review`)
    assert.equal(can(role, 'schoolFilter'), true, `${role} school filter`)
  }
  assert.equal(can('user', 'verify'), false)
  assert.equal(can('user', 'review'), false)
  assert.equal(can('user', 'schoolFilter'), false)
})

test('an unknown role may do nothing', () => {
  for (const action of ['sign', 'verify', 'review', 'schoolFilter']) {
    assert.equal(can(null, action), false, `${action} without a role`)
    assert.equal(can(undefined, action), false, `${action} without a role`)
  }
})

test('denyReason is empty when allowed and names the cause when denied', () => {
  assert.equal(denyReason('master', 'verify'), null)
  assert.equal(denyReason('user', 'sign'), null)

  assert.match(denyReason('user', 'verify'), /管理员/)
  assert.match(denyReason('user', 'review'), /管理员/)
  assert.match(denyReason('user', 'schoolFilter'), /管理员/)
  assert.match(denyReason(null, 'sign'), /会话/)
  assert.match(denyReason(undefined, 'verify'), /会话/)
})

function stubFetchPages(pages) {
  const calls = []
  const original = globalThis.fetch
  let index = 0
  globalThis.fetch = async (input) => {
    calls.push(String(input))
    const page = pages[Math.min(index, pages.length - 1)]
    index += 1
    return new Response(JSON.stringify(page), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  return {
    calls,
    restore() {
      globalThis.fetch = original
    },
  }
}

test('listAllSchools follows next_cursor with the maximum page size', async () => {
  const capture = stubFetchPages([
    { items: [{ id: 'school-a' }], next_cursor: 'c2' },
    { items: [{ id: 'school-b' }], next_cursor: '' },
  ])
  let schools
  try {
    schools = await listAllSchools()
  } finally {
    capture.restore()
  }

  assert.deepEqual(
    schools.map((school) => school.id),
    ['school-a', 'school-b'],
  )
  assert.equal(capture.calls.length, 2)

  const first = new URL(capture.calls[0], 'http://probe')
  const second = new URL(capture.calls[1], 'http://probe')
  assert.equal(first.pathname, '/api/schools')
  assert.equal(first.searchParams.get('limit'), '200')
  assert.equal(first.searchParams.get('cursor'), null)
  assert.equal(second.searchParams.get('cursor'), 'c2')
  assert.equal(second.searchParams.get('limit'), '200')
})

test('listAllSchools stops at the ten page cap', async () => {
  const pages = Array.from({ length: 12 }, (_, index) => ({
    items: [{ id: `school-${index}` }],
    next_cursor: `c${index + 1}`,
  }))
  const capture = stubFetchPages(pages)
  let schools
  try {
    schools = await listAllSchools()
  } finally {
    capture.restore()
  }
  assert.equal(capture.calls.length, 10)
  assert.equal(schools.length, 10)
})
