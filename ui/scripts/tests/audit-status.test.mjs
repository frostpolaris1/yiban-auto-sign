import assert from 'node:assert/strict'
import test from 'node:test'

import {
  auditBrokenText,
  auditVerdict,
} from '../../src/features/dashboard/audit-status.ts'

/**
 * 审计巡检卡的三态与锚点判定。
 *
 * 锚点不符/错误优先于 empty：空链带锚点告警不得被洗成「暂无审计行」。
 * missing 不改变链结论：页面用黄条提示「巡检仅链内自洽」。
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

test('anchor missing keeps the chain verdict', () => {
  assert.equal(auditVerdict(report({ anchor_status: 'missing' })), 'ok')
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

test('missing anchor has no broken text', () => {
  assert.equal(auditBrokenText(report({ anchor_status: 'missing' })), null)
})
