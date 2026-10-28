import assert from 'node:assert/strict'
import test from 'node:test'

import { capacitySearch } from '../../src/lib/api/schedule.ts'
import {
  CAPACITY_SOURCE_LABELS,
  SUGGESTION_LABELS,
  SUGGESTION_TONES,
  STATE_LABELS,
  capacityOptions,
  capacitySourceTone,
  signCounts,
  signKpiCards,
  stateLabel,
  windowCounts,
  windowKpiCards,
} from '../../src/features/dashboard/display.ts'
import { SOURCE_LABELS, sourceTone } from '../../src/features/schedule/display.ts'

/**
 * 看板纯逻辑的不变式。
 *
 * KPI 由条目派生，不另造统计端点；state=null 计入「无任务」，不进其余三项。
 * 状态标签取契约词汇，未知值原样显示：契约漂移必须可见。
 * 建议标签四值穷尽，键集与契约枚举相同。
 * 容量选项只取「启用且有窗口」的行；label 是 `学校名 · 任务名`。
 * 容量查询串带 school_id、task_id、day；refresh 只在要求时出现。
 * 看板与服务页的来源标签必须逐字相同：两处同形不得漂移。
 */

const DAY = '2026-10-28'

function signItem(state, stale = false) {
  return { account_id: 'acc-0001', state, run_at: null, stale }
}

function window(overrides) {
  return {
    school_id: 'school-alpha',
    school_name: '合成学校甲',
    task_id: 'task-daily',
    task_name: '每日签到',
    enabled: true,
    day: DAY,
    start: '2026-10-28T08:30:00+08:00',
    end: '2026-10-28T09:00:00+08:00',
    source: 'platform',
    version: 2,
    ...overrides,
  }
}

test('sign counts group every contract state exactly once', () => {
  assert.deepEqual(signCounts([]), {
    total: 0,
    success: 0,
    active: 0,
    failed: 0,
    noTask: 0,
  })

  const one = signCounts([signItem('success')])
  assert.equal(one.total, 1)
  assert.equal(one.success, 1)

  const many = signCounts([
    signItem('success'),
    signItem('success'),
    signItem('pending'),
    signItem('running'),
    signItem('failed'),
    signItem('skipped'),
    signItem(null),
  ])
  assert.deepEqual(many, { total: 7, success: 2, active: 2, failed: 2, noTask: 1 })
  assert.equal(
    many.total,
    many.success + many.active + many.failed + many.noTask,
    'every known state lands in exactly one bucket',
  )

  // 未知 state（契约是 string | null，不排除服务端漂移）只计入 total；
  // 四桶之和小于 total 是故意的：合计差让漂移在界面上露头。
  const unknown = signCounts([signItem('archived')])
  assert.equal(unknown.total, 1)
  assert.equal(unknown.success + unknown.active + unknown.failed + unknown.noTask, 0)
})

test('the KPI cards carry the derived numbers and an em dash without data', () => {
  const cards = signKpiCards({ total: 7, success: 2, active: 2, failed: 2, noTask: 1 })
  assert.equal(cards.length, 4)
  assert.deepEqual(
    cards.map((card) => card.value),
    [7, 2, 2, 2],
  )
  assert.equal(cards[0].label, '账号总数')
  assert.match(cards[0].note, /无任务 1/)

  const blank = signKpiCards(null)
  assert.deepEqual(
    blank.map((card) => card.value),
    [null, null, null, null],
    'no data means no invented numbers',
  )
})

test('window counts split reported, disabled and usable rows', () => {
  assert.deepEqual(windowCounts([]), { total: 0, reported: 0, disabled: 0, usable: 0 })

  const rows = [
    window({}),
    window({ school_id: 'school-beta', task_id: 'task-night' }),
    window({ school_id: 'school-gamma', task_id: 'task-weekly', enabled: false }),
    window({ school_id: 'school-beta', task_id: 'task-legacy', start: null, end: null, source: null }),
  ]
  assert.deepEqual(windowCounts(rows), { total: 4, reported: 3, disabled: 1, usable: 2 })

  const cards = windowKpiCards({ total: 4, reported: 3, disabled: 1, usable: 2 })
  assert.deepEqual(
    cards.map((card) => card.value),
    [4, 3, 1, 2],
  )
})

test('every contract state has a label and null means no task', () => {
  assert.deepEqual(Object.keys(STATE_LABELS).sort(), [
    'failed',
    'pending',
    'running',
    'skipped',
    'success',
  ])
  assert.equal(stateLabel(null), '无任务')
  assert.equal(stateLabel('pending'), '待签')
  assert.equal(stateLabel('running'), '进行中')
  assert.equal(stateLabel('success'), '已签')
  assert.equal(stateLabel('failed'), '失败')
  assert.equal(stateLabel('skipped'), '跳过')
  assert.equal(stateLabel('archived'), 'archived', 'contract drift stays visible')
})

test('the four suggestion labels cover the contract enum exactly', () => {
  assert.deepEqual(SUGGESTION_LABELS, {
    wait: '等待',
    retry_now: '即将重试',
    re_arm: '需重新排程',
    contact_admin: '联系管理员',
  })
  assert.deepEqual(Object.keys(SUGGESTION_TONES).sort(), Object.keys(SUGGESTION_LABELS).sort())
})

test('capacity options keep enabled rows with a reported window only', () => {
  const rows = [
    window({}),
    window({
      school_id: 'school-beta',
      school_name: '合成学校乙',
      task_id: 'task-night',
      task_name: '夜间签到',
    }),
    window({ school_id: 'school-gamma', task_id: 'task-weekly', enabled: false }),
    window({ school_id: 'school-beta', task_id: 'task-legacy', start: null, end: null }),
  ]
  const options = capacityOptions(rows)
  assert.deepEqual(
    options.map((option) => option.value),
    ['school-alpha:task-daily', 'school-beta:task-night'],
  )
  assert.equal(options[0].label, '合成学校甲 · 每日签到')
  assert.equal(options[1].label, '合成学校乙 · 夜间签到')
  assert.deepEqual(capacityOptions([]), [])
})

test('the capacity query carries both ids and the day, refresh only on demand', () => {
  assert.equal(
    capacitySearch({ day: DAY, schoolId: 'school-alpha', taskId: 'task-daily' }),
    `?school_id=school-alpha&task_id=task-daily&day=${DAY}`,
  )
  assert.equal(
    capacitySearch({ day: DAY, schoolId: 'school-alpha', taskId: 'task-daily', refresh: true }),
    `?school_id=school-alpha&task_id=task-daily&day=${DAY}&refresh=true`,
  )
  assert.match(
    capacitySearch({ day: DAY, schoolId: 'a b', taskId: 't&x' }),
    /school_id=a\+b&task_id=t%26x&day=/,
  )
})

test('the capacity card names window sources exactly like the schedule page', () => {
  assert.deepEqual(CAPACITY_SOURCE_LABELS, SOURCE_LABELS)
  for (const source of ['platform', 'cache', 'fallback', 'override']) {
    assert.equal(capacitySourceTone(source), sourceTone(source), `${source} tone must match`)
  }
})
