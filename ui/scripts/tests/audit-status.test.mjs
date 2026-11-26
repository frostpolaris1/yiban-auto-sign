import assert from 'node:assert/strict'
import test from 'node:test'

import {
  auditBrokenText,
  auditVerdict,
  healthForcedKey,
  resolveHealthOpen,
} from '../../src/features/dashboard/audit-status.ts'

/**
 * 审计巡检卡的三态与锚点判定。
 *
 * 锚点不符/错误优先于 empty：空链带锚点告警不得被洗成「暂无审计行」。
 * missing 是失败关闭：服务端对缺失锚点答 ok=false，页面判「链异常」，
 * 行内文案提示「锚点缺失（尚未初始化）」。
 */

function report(overrides = {}) {
  return {
    ok: true,
    empty: false,
    checked_rows: 3,
    last_hash: '',
    broken_at: null,
    checked_at: '2026-11-11T00:00:00Z',
    ...overrides,
  }
}

test('anchor mismatch wins over empty', () => {
  assert.equal(
    auditVerdict(report({ ok: false, empty: true, anchor_status: 'mismatch' })),
    'broken',
  )
})

test('anchor missing is broken even with a passing chain', () => {
  assert.equal(auditVerdict(report({ ok: false, anchor_status: 'missing' })), 'broken')
})

test('anchor error is broken', () => {
  assert.equal(auditVerdict(report({ anchor_status: 'error' })), 'broken')
})

test('anchor mismatch reason is shown', () => {
  assert.equal(
    auditBrokenText(report({ anchor_status: 'mismatch', anchor_reason: 'anchor_truncated' })),
    'anchor_truncated',
  )
})

test('missing anchor explains initialization', () => {
  assert.equal(
    auditBrokenText(report({ anchor_status: 'missing', anchor_reason: 'anchor_not_initialized' })),
    'anchor_not_initialized',
  )
})

test('missing anchor stays broken on an empty chain', () => {
  assert.equal(
    auditVerdict(report({ ok: false, empty: true, anchor_status: 'missing' })),
    'broken',
  )
})

// 这三例只锁纯函数语义；组件是否真的调用它们由 e2e「B5: a broken audit chain
// force-expands the mobile strip」在真实 DOM 上咬住（helper 被绕开时该用例会红）。
test('health strip keeps a manual collapse within the same anomaly signature', () => {
  const key = healthForcedKey(null, null, 'engine offline')
  assert.equal(resolveHealthOpen(true, key, { key, open: false }), false)
})

test('health strip reopens when the anomaly signature changes', () => {
  const oldKey = healthForcedKey(null, '500 巡检不可用', null)
  const newKey = healthForcedKey('broken_at row 3', null, null)
  assert.equal(resolveHealthOpen(true, newKey, { key: oldKey, open: false }), true)
})

test('health strip falls back to forced state without an override', () => {
  const key = healthForcedKey('broken_at row 3', null, null)
  assert.equal(resolveHealthOpen(true, key, null), true)
  assert.equal(resolveHealthOpen(false, key, null), false)
})
