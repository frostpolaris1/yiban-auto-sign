import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  TASK_STATES,
  TASK_STATE_LABELS,
} from '../../src/lib/identity.ts'
import { SIGN_STATES, STATE_LABELS } from '../../src/features/dashboard/display.ts'
import {
  parseRunSearchRecord,
  runSearchRecord,
  withRunFilter,
} from '../../src/features/run/urlState.ts'

/**
 * 运行页的不变式。
 *
 * 一、任务状态文案只有一份事实：`lib/identity` 的共用词表。
 *     运行页引用它；看板的同组词在单元测试里钉住相等，
 *     两处不得各写一份（看板属已上线页面，本批不改）。
 * 二、URL 状态：解析丢弃非法值（校验即净化）、空值不产生键、
 *     换筛选统一丢游标。刷新与分享靠它。
 */

test('run page task words are the shared identity words', () => {
  // 运行页不自带词表：features/run/display.ts 不得出现状态字面量文案。
  const source = readFileSync(
    new URL('../../src/features/run/display.ts', import.meta.url),
    'utf8',
  )
  for (const word of Object.values(TASK_STATE_LABELS)) {
    assert.ok(
      !source.includes(`'${word}'`),
      `${word} must not be restated in features/run/display.ts`,
    )
  }
})

test('the shared task words match the dashboard vocabulary', () => {
  // 看板词表先于运行页存在。两表取值域与文案都必须相等：
  // 未来任何一侧改词，本测试强制同批对齐。
  assert.deepEqual(TASK_STATES, SIGN_STATES)
  assert.deepEqual(TASK_STATE_LABELS, STATE_LABELS)
})

// --- URL 状态 ---------------------------------------------------------------

test('parseRunSearchRecord keeps only valid values', () => {
  const state = parseRunSearchRecord({
    day: '2026-10-31',
    state: 'failed',
    school: 'school-a',
    q: '13800138',
    cursor: 'c1',
  })
  assert.deepEqual(state, {
    day: '2026-10-31',
    state: 'failed',
    school: 'school-a',
    q: '13800138',
    cursor: 'c1',
  })
})

test('parseRunSearchRecord drops malformed values', () => {
  const state = parseRunSearchRecord({
    day: '31/10/2026',
    state: 'done',
    school: '',
    q: '',
    cursor: '',
    unknown: 'x',
  })
  assert.deepEqual(state, {})
})

test('runSearchRecord omits empty values so same state reads same URL', () => {
  assert.deepEqual(runSearchRecord({}), {})
  assert.deepEqual(
    runSearchRecord({ day: '2026-10-31', state: 'failed' }),
    { day: '2026-10-31', state: 'failed' },
  )
  assert.deepEqual(
    runSearchRecord({ day: '2026-10-31', q: 'school', cursor: 'c2' }),
    { day: '2026-10-31', q: 'school', cursor: 'c2' },
  )
})

test('withRunFilter resets the cursor on any filter change', () => {
  const next = withRunFilter(
    { day: '2026-10-31', state: 'failed', cursor: 'c2' },
    { state: undefined },
  )
  assert.deepEqual(next, { day: '2026-10-31' })

  const same = withRunFilter({ day: '2026-10-31' }, { day: '2026-10-31' })
  assert.deepEqual(same, { day: '2026-10-31' })
})

test('parse and serialize round-trip the same state', () => {
  const record = { day: '2026-10-31', state: 'pending', q: '甲校', cursor: 'c3' }
  assert.deepEqual(runSearchRecord(parseRunSearchRecord(record)), record)
})
