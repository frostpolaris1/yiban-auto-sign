import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { TASK_STATE_LABELS } from '../../src/lib/identity.ts'
import {
  accountStatusMeta,
  diagnosticsSuggestionMeta,
  formatBusinessDayLabel,
  formatWindowSummary,
  schoolNameMap,
  taskStateMeta,
} from '../../src/features/queue/queueDisplay.ts'

test('queueDisplay uses shared identity vocabulary for task states', () => {
  const source = readFileSync(
    new URL('../../src/features/queue/queueDisplay.ts', import.meta.url),
    'utf8',
  )
  for (const word of Object.values(TASK_STATE_LABELS)) {
    assert.ok(
      !source.includes(`'${word}'`),
      `${word} must come from lib/identity rather than being restated in queueDisplay.ts`,
    )
  }
  assert.equal(taskStateMeta('failed').label, '失败')
  assert.equal(taskStateMeta('pending').label, '待签')
  assert.equal(taskStateMeta('running').label, '进行中')
})

test('formatBusinessDayLabel formats Shanghai weekday and handles invalid input', () => {
  const valid = formatBusinessDayLabel('2026-10-31')
  assert.equal(valid.date, '2026-10-31')
  assert.match(valid.weekday, /周六/)

  const invalid = formatBusinessDayLabel('not-a-day')
  assert.equal(invalid.date, 'not-a-day')
  assert.equal(invalid.weekday, '')
})

test('formatWindowSummary handles empty, weekend_closed, single and multiple windows', () => {
  assert.equal(formatWindowSummary([]).label, '未配置')

  const weekend = formatWindowSummary([
    {
      school_id: 's1',
      school_name: '甲',
      task_id: 't1',
      task_name: '早签',
      enabled: true,
      day: '2026-10-31',
      start: null,
      end: null,
      source: 'weekend_closed',
      version: 1,
    },
  ])
  assert.equal(weekend.label, '今日休窗')
  assert.equal(weekend.closed, true)

  const single = formatWindowSummary([
    {
      school_id: 's1',
      school_name: '合成学校甲',
      task_id: 't1',
      task_name: '早签到',
      enabled: true,
      day: '2026-10-31',
      start: '2026-10-31T08:30:00+08:00',
      end: '2026-10-31T09:00:00+08:00',
      source: 'cache',
      version: 1,
    },
  ])
  assert.equal(single.label, '08:30 — 09:00')
  assert.equal(single.detail, '合成学校甲 · 早签到')

  const multi = formatWindowSummary([
    {
      school_id: 's1',
      school_name: '甲',
      task_id: 't1',
      task_name: '早',
      enabled: true,
      day: '2026-10-31',
      start: '2026-10-31T08:30:00+08:00',
      end: '2026-10-31T09:00:00+08:00',
      source: 'cache',
      version: 1,
    },
    {
      school_id: 's2',
      school_name: '乙',
      task_id: 't2',
      task_name: '晚',
      enabled: true,
      day: '2026-10-31',
      start: '2026-10-31T20:00:00+08:00',
      end: '2026-10-31T21:00:00+08:00',
      source: 'platform',
      version: 1,
    },
  ])
  assert.equal(multi.label, '08:30 — 21:00')
  assert.match(multi.detail, /2 个/)
})

test('accountStatusMeta, diagnosticsSuggestionMeta and schoolNameMap return consistent labels', () => {
  assert.equal(accountStatusMeta('pending').label, '待审核')
  assert.equal(accountStatusMeta('rejected').label, '已驳回')
  assert.equal(diagnosticsSuggestionMeta('retry_now').label, '可以立即重试')
  const map = schoolNameMap([
    {
      id: 'school-a',
      code: 'demo-a',
      name: '合成学校甲',
      status: 'active',
      timezone: 'Asia/Shanghai',
      created_at: '2026-10-01T00:00:00Z',
      updated_at: '2026-10-01T00:00:00Z',
    },
  ])
  assert.equal(map.get('school-a'), '合成学校甲')
})
