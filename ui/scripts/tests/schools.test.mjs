import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildSchoolCreate,
  buildSchoolUpdate,
  schoolCodeError,
  schoolCreateError,
  schoolUpdateError,
} from '../../src/features/schools/drafts.ts'
import { can, denyReason } from '../../src/features/schools/permissions.ts'
import { visibleNav } from '../../src/components/shell/nav.ts'
import { listAllSchoolAdmins } from '../../src/lib/api/school.ts'

/**
 * 学校域纯逻辑的不变式。
 *
 * code：契约 pattern `^[a-z0-9][a-z0-9-]{1,31}$`，创建后不可改。
 * create：只带契约字段；名称 1..128；时区仅 Asia/Shanghai。
 * update：只带变更字段；无变更返回空对象；code 永不出现。
 * 谓词：view 归 master/admin，create/edit/admins 只归 master。
 * 导航：user 无学校入口；其余页面照旧可见。
 * 管理员列表：按 next_cursor 取全，封顶 10 页，单页 200。
 */

const CURRENT = {
  id: 'school-alpha',
  code: 'syn-alpha',
  name: '合成学校甲',
  status: 'active',
  timezone: 'Asia/Shanghai',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

test('school codes follow the contract pattern', () => {
  for (const code of ['ab', 'a1', 'a-b', 'syn-alpha', `a${'b'.repeat(31)}`]) {
    assert.equal(schoolCodeError(code), null, code)
  }

  assert.match(schoolCodeError(''), /必填/)
  assert.match(schoolCodeError(' syn-alpha '), /小写/, 'the raw value is validated')
  assert.match(schoolCodeError('a'), /2–32/)
  assert.match(schoolCodeError(`a${'b'.repeat(32)}`), /2–32/)
  assert.match(schoolCodeError('Abc'), /小写/)
  assert.match(schoolCodeError('a_b'), /小写/)
  assert.match(schoolCodeError('-ab'), /小写/)
  assert.match(schoolCodeError('a b'), /小写/)
})

test('create trims the draft and carries only contract fields', () => {
  const body = buildSchoolCreate({
    code: ' syn-gamma ',
    name: ' 合成学校丙 ',
    timezone: 'Asia/Shanghai',
  })
  assert.deepEqual(body, {
    code: 'syn-gamma',
    name: '合成学校丙',
    timezone: 'Asia/Shanghai',
  })
  assert.deepEqual(Object.keys(body).sort(), ['code', 'name', 'timezone'])

  assert.throws(() => buildSchoolCreate({ code: 'Bad', name: '合成', timezone: 'Asia/Shanghai' }))
})

test('create refuses a missing name and an unsupported time zone', () => {
  assert.match(
    schoolCreateError({ code: 'syn', name: '   ', timezone: 'Asia/Shanghai' }),
    /名称/,
  )
  assert.match(
    schoolCreateError({ code: 'syn', name: 'x'.repeat(129), timezone: 'Asia/Shanghai' }),
    /128/,
  )
  assert.match(schoolCreateError({ code: 'syn', name: '合成', timezone: 'UTC' }), /Asia\/Shanghai/)
})

test('update builds only the changed fields', () => {
  assert.deepEqual(
    buildSchoolUpdate(CURRENT, currentDraft()),
    {},
    'an unchanged draft sends nothing',
  )
  assert.deepEqual(
    buildSchoolUpdate(CURRENT, { ...currentDraft(), name: ' 合成学校甲二 ' }),
    { name: '合成学校甲二' },
  )
  assert.deepEqual(
    buildSchoolUpdate(CURRENT, { ...currentDraft(), status: 'disabled' }),
    { status: 'disabled' },
  )
  assert.deepEqual(
    buildSchoolUpdate(CURRENT, { ...currentDraft(), status: 'disabled', name: '合成学校甲二' }),
    { status: 'disabled', name: '合成学校甲二' },
  )

  const keys = Object.keys(buildSchoolUpdate(CURRENT, { ...currentDraft(), name: '新名' }))
  assert.deepEqual(keys, ['name'], 'code is never part of an update')
})

test('update refuses an empty name and an unsupported time zone', () => {
  assert.match(schoolUpdateError(CURRENT, { ...currentDraft(), name: '  ' }), /名称/)
  assert.match(
    schoolUpdateError(CURRENT, { ...currentDraft(), name: 'x'.repeat(129) }),
    /128/,
  )
  assert.match(
    schoolUpdateError(CURRENT, { ...currentDraft(), timezone: 'UTC' }),
    /Asia\/Shanghai/,
  )
  assert.equal(
    schoolUpdateError({ ...CURRENT, timezone: 'UTC' }, { ...currentDraft(), timezone: 'UTC' }),
    null,
    'an untouched legacy time zone does not block other edits',
  )
  assert.throws(() => buildSchoolUpdate(CURRENT, { ...currentDraft(), name: '' }))
})

function currentDraft() {
  return { name: CURRENT.name, status: CURRENT.status, timezone: CURRENT.timezone }
}

const ACTION_MATRIX = {
  master: { view: true, create: true, edit: true, admins: true },
  admin: { view: true, create: false, edit: false, admins: false },
  user: { view: false, create: false, edit: false, admins: false },
}

const ACTIONS = ['view', 'create', 'edit', 'admins']

test('the four school actions follow the contract x-roles', () => {
  for (const [role, row] of Object.entries(ACTION_MATRIX)) {
    for (const action of ACTIONS) {
      assert.equal(can(role, action), row[action], `${role} ${action}`)
    }
  }
  for (const action of ACTIONS) {
    assert.equal(can(null, action), false, `no role ${action}`)
    assert.equal(can(undefined, action), false, `no role ${action}`)
  }
})

test('school denials name the missing role', () => {
  assert.equal(denyReason('master', 'create'), null)
  assert.equal(denyReason('admin', 'view'), null)

  assert.match(denyReason('user', 'view'), /你的角色无权查看学校管理/)
  assert.match(denyReason('user', 'create'), /仅 master 可新建学校/)
  assert.match(denyReason('admin', 'create'), /仅 master 可新建学校/)
  assert.match(denyReason('admin', 'edit'), /仅 master 可编辑/)
  assert.match(denyReason('admin', 'admins'), /仅 master 可管理学校管理员/)
  assert.match(denyReason(null, 'view'), /会话角色未识别/)
  assert.match(denyReason(undefined, 'edit'), /会话角色未识别/)
})

function navTargets(role) {
  return visibleNav(role)
    .flatMap((section) => section.items)
    .flatMap((item) => ('children' in item ? item.children : [item]))
    .map((leaf) => leaf.to)
}

test('the school nav item is hidden from users and shown to managers', () => {
  for (const role of ['master', 'admin']) {
    assert.ok(navTargets(role).includes('/schools'), `${role} sees schools`)
  }
  assert.ok(!navTargets('user').includes('/schools'), 'user has no school entry')
  assert.ok(!navTargets(null).includes('/schools'), 'no role has no school entry')
  assert.ok(navTargets('user').includes('/accounts'), 'other pages stay visible')
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

test('listAllSchoolAdmins follows next_cursor with the maximum page size', async () => {
  const capture = stubFetchPages([
    { items: [{ user_id: 'user-1' }], next_cursor: 'c2' },
    { items: [{ user_id: 'user-2' }], next_cursor: '' },
  ])
  let admins
  try {
    admins = await listAllSchoolAdmins('school-alpha')
  } finally {
    capture.restore()
  }

  assert.deepEqual(
    admins.map((admin) => admin.user_id),
    ['user-1', 'user-2'],
  )
  assert.equal(capture.calls.length, 2)

  const first = new URL(capture.calls[0], 'http://probe')
  const second = new URL(capture.calls[1], 'http://probe')
  assert.equal(first.pathname, '/api/schools/school-alpha/admins')
  assert.equal(first.searchParams.get('limit'), '200')
  assert.equal(first.searchParams.get('cursor'), null)
  assert.equal(second.searchParams.get('cursor'), 'c2')
  assert.equal(second.searchParams.get('limit'), '200')
})

test('listAllSchoolAdmins stops at the ten page cap', async () => {
  const pages = Array.from({ length: 12 }, (_, index) => ({
    items: [{ user_id: `user-${index}` }],
    next_cursor: `c${index + 1}`,
  }))
  const capture = stubFetchPages(pages)
  let admins
  try {
    admins = await listAllSchoolAdmins('school-alpha')
  } finally {
    capture.restore()
  }
  assert.equal(capture.calls.length, 10)
  assert.equal(admins.length, 10)
})
