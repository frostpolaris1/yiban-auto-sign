import assert from 'node:assert/strict'
import test from 'node:test'

import {
  accountUpdateError,
  buildAccountUpdate,
  buildLocationUpdate,
  locationError,
} from '../../src/features/accounts/drafts.ts'
import { canUseMenu, menuDenyReason } from '../../src/features/accounts/permissions.ts'

/**
 * 切片 2 纯逻辑不变式。
 *
 * PATCH：只带填写字段；空串视为未填；非法手机号过不去。
 * location：auto 不带经纬度；fixed 过 WGS84 范围；理由只在不空时携带。
 * 行菜单：edit 与 delete 对全体开放；location 只归 user/admin。
 */

test('buildAccountUpdate keeps only the filled fields', () => {
  assert.deepEqual(buildAccountUpdate({}), {})
  assert.deepEqual(
    buildAccountUpdate({ phone: '   ', password: '', device_code: '  ', phone_model: '' }),
    {},
  )
  assert.deepEqual(
    buildAccountUpdate({ phone: ' 13800138001 ', device_code: ' dev-1 ' }),
    { phone: '13800138001', device_code: 'dev-1' },
  )
  assert.deepEqual(
    buildAccountUpdate({ password: ' pw ', phone_model: ' iOS 18 ' }),
    { password: ' pw ', phone_model: 'iOS 18' },
  )
})

test('the password is sent verbatim, never trimmed', () => {
  // 服务端与登录表单都不动空格：trim 会静默改写用户输入的凭据。
  assert.deepEqual(buildAccountUpdate({ password: '  pass word  ' }), { password: '  pass word  ' })
  assert.deepEqual(buildAccountUpdate({ password: 'pw' }), { password: 'pw' })
})

test('an invalid phone is refused with a named cause', () => {
  assert.match(accountUpdateError({ phone: '123' }), /11 位/)
  assert.match(accountUpdateError({ phone: '23800138001' }), /11 位/)
  assert.match(accountUpdateError({ phone: '1380013800' }), /11 位/)
  assert.match(accountUpdateError({ phone: '138001380010' }), /11 位/)

  assert.equal(accountUpdateError({ phone: '13800138001' }), null)
  assert.equal(accountUpdateError({ device_code: 'dev-1' }), null)
  assert.equal(accountUpdateError({}), null)

  assert.throws(() => buildAccountUpdate({ phone: '123' }))
})

test('location auto drops coordinates and reason', () => {
  assert.deepEqual(
    buildLocationUpdate({ mode: 'auto', lng: '200', lat: '95', reason: '合成' }),
    { mode: 'auto' },
  )
  assert.equal(locationError({ mode: 'auto', lng: '', lat: '' }), null)
})

test('location fixed validates the WGS84 ranges', () => {
  assert.match(locationError({ mode: 'fixed', lng: '', lat: '2.5' }), /必填/)
  assert.match(locationError({ mode: 'fixed', lng: '200', lat: '2.5' }), /经度/)
  assert.match(locationError({ mode: 'fixed', lng: '-1.5', lat: '95' }), /纬度/)
  assert.match(locationError({ mode: 'fixed', lng: 'abc', lat: '2.5' }), /经度/)
  assert.match(
    locationError({ mode: 'fixed', lng: '-1.5', lat: '2.5', reason: 'x'.repeat(201) }),
    /200/,
  )

  assert.equal(locationError({ mode: 'fixed', lng: '-180', lat: '90' }), null)
  assert.equal(locationError({ mode: 'fixed', lng: '180', lat: '-90' }), null)
  assert.equal(locationError({ mode: 'fixed', lng: '0', lat: '0' }), null)
  assert.equal(locationError({ mode: 'fixed', lng: '-1.5', lat: '2.5', reason: '合成' }), null)
})

test('location fixed carries the point and the trimmed reason', () => {
  assert.deepEqual(
    buildLocationUpdate({ mode: 'fixed', lng: ' 1.5 ', lat: ' 2.5 ', reason: ' 合成 ' }),
    { mode: 'fixed', lng: 1.5, lat: 2.5, reason: '合成' },
  )
  assert.deepEqual(
    buildLocationUpdate({ mode: 'fixed', lng: '0', lat: '0', reason: '' }),
    { mode: 'fixed', lng: 0, lat: 0 },
  )
  assert.throws(() => buildLocationUpdate({ mode: 'fixed', lng: '200', lat: '2.5' }))
})

const MENU_CASES = {
  master: { edit: true, location: false, delete: true },
  admin: { edit: true, location: true, delete: true },
  user: { edit: true, location: true, delete: true },
}

test('the row menu follows the endpoint roles', () => {
  for (const [role, row] of Object.entries(MENU_CASES)) {
    for (const item of ['edit', 'location', 'delete']) {
      assert.equal(canUseMenu(role, item), row[item], `${role} ${item}`)
    }
  }
  for (const item of ['edit', 'location', 'delete']) {
    assert.equal(canUseMenu(null, item), false, `no role ${item}`)
    assert.equal(canUseMenu(undefined, item), false, `no role ${item}`)
  }
})

test('menu denials name the cause', () => {
  assert.match(menuDenyReason('master', 'location'), /user\/admin/)
  assert.match(menuDenyReason(null, 'edit'), /会话/)
  assert.match(menuDenyReason(null, 'location'), /会话/)
  assert.match(menuDenyReason(null, 'delete'), /会话/)

  assert.equal(menuDenyReason('admin', 'location'), null)
  assert.equal(menuDenyReason('user', 'edit'), null)
  assert.equal(menuDenyReason('user', 'delete'), null)
})
