import assert from 'node:assert/strict'
import test from 'node:test'

import { capacitySearch } from '../../src/lib/api/schedule.ts'
import { dayOf } from '../../src/lib/format.ts'
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
  todayKpiCards,
  todayStats,
  windowCounts,
} from '../../src/features/dashboard/display.ts'
import { SOURCE_LABELS, sourceTone } from '../../src/features/schedule/display.ts'

/**
 * 看板纯逻辑的不变式。
 *
 * KPI 由条目派生，不另造统计端点；state=null 计入「无任务」，不进其余三项。
 * 今日终态口径：取报告末两天；成功率＝成功÷（成功+失败），跳过不计入；
 * 分母 0 为 null；pill 分「有对比/无对比」两态；报告未到或 days 为空不编数字。
 * 缓存键的业务日按上海时区取（dayOf）：+08:00 的 23:59 与 00:00 分属两日。
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
})

// ---------- master/admin 的今日终态口径 ----------

function statsWithDays(lastTwoDays) {
  return {
    days: [
      { day: '2026-10-01', pending: 0, running: 0, success: 3, failed: 1, skipped: 0 },
      ...lastTwoDays,
    ],
    total: 4,
    by_status: { pending: 1, running: 0, success: 3, failed: 1, skipped: 1 },
    days_missing: [],
  }
}

test('today stats read the last two entries and exclude skipped from the rate', () => {
  const stats = todayStats(
    statsWithDays([
      { day: '2026-10-27', pending: 0, running: 0, success: 4, failed: 4, skipped: 2 },
      { day: '2026-10-28', pending: 3, running: 1, success: 5, failed: 2, skipped: 9 },
    ]),
  )
  assert.equal(stats.success, 5)
  assert.equal(stats.failed, 2)
  assert.equal(stats.active, 4, 'running and pending both count as in flight')
  assert.equal(stats.rate, 71.4, '5 ÷ 7 to one decimal; nine skips stay out of the math')
  assert.deepEqual(stats.pill, { text: '较昨日 +21.4pp', cls: 'up' })
})

test('the rate pill covers flat, down and no-comparison states', () => {
  const flat = todayStats(
    statsWithDays([
      { day: '2026-10-27', pending: 0, running: 0, success: 3, failed: 0, skipped: 0 },
      { day: '2026-10-28', pending: 0, running: 0, success: 3, failed: 0, skipped: 4 },
    ]),
  )
  assert.deepEqual(flat.pill, { text: '较昨日 0pp', cls: 'flat' })

  const down = todayStats(
    statsWithDays([
      { day: '2026-10-27', pending: 0, running: 0, success: 4, failed: 1, skipped: 0 },
      { day: '2026-10-28', pending: 0, running: 0, success: 2, failed: 3, skipped: 0 },
    ]),
  )
  assert.deepEqual(down.pill, { text: '较昨日 -40pp', cls: 'down' })

  const noComparison = todayStats(
    statsWithDays([
      { day: '2026-10-27', pending: 1, running: 0, success: 0, failed: 0, skipped: 5 },
      { day: '2026-10-28', pending: 0, running: 0, success: 3, failed: 3, skipped: 0 },
    ]),
  )
  assert.equal(noComparison.rate, 50)
  assert.deepEqual(noComparison.pill, { text: '无昨日对比', cls: 'flat' })
})

test('a zero denominator and a missing report show dashes, not invented numbers', () => {
  const zero = todayStats(
    statsWithDays([
      { day: '2026-10-27', pending: 0, running: 0, success: 1, failed: 0, skipped: 0 },
      { day: '2026-10-28', pending: 2, running: 0, success: 0, failed: 0, skipped: 0 },
    ]),
  )
  assert.equal(zero.rate, null)
  assert.equal(zero.pill, null, 'no rate means no comparison either')
  assert.equal(zero.success, 0)
  assert.equal(zero.active, 2)

  // 日期缺失按 0 桶处理，不编造。
  const short = todayStats({
    days: [{ day: '2026-10-28', pending: 0, running: 0, success: 2, failed: 1, skipped: 0 }],
    total: 1,
    by_status: { pending: 0, running: 0, success: 2, failed: 1, skipped: 0 },
    days_missing: [],
  })
  assert.deepEqual(
    { rate: short.rate, pill: short.pill, active: short.active },
    { rate: 66.7, pill: { text: '无昨日对比', cls: 'flat' }, active: 0 },
  )

  assert.equal(todayStats(null), null)
  assert.equal(todayStats(undefined), null)

  // days 为空：报告在但没有终态，仍是 null，四卡「—」而不是编出来的 0。
  assert.equal(
    todayStats({
      days: [],
      total: 0,
      by_status: { pending: 0, running: 0, success: 0, failed: 0, skipped: 0 },
      days_missing: [],
    }),
    null,
  )
})

test('the business day for the stats key rolls over at Shanghai midnight', () => {
  // 复用 lib/format 的 dayOf（上海时区），与 sign-stats 服务端窗口终点同口径。
  assert.equal(dayOf(new Date('2026-11-03T15:59:59Z')), '2026-11-03', '+08:00 的 23:59')
  assert.equal(dayOf(new Date('2026-11-03T16:00:00Z')), '2026-11-04', '+08:00 的 00:00')
})

test('the manager KPI cards carry today numbers and an em dash without data', () => {
  const stats = todayStats(
    statsWithDays([
      { day: '2026-10-27', pending: 0, running: 0, success: 4, failed: 4, skipped: 0 },
      { day: '2026-10-28', pending: 1, running: 1, success: 5, failed: 2, skipped: 0 },
    ]),
  )
  const cards = todayKpiCards(stats)
  assert.equal(cards.length, 4)
  assert.deepEqual(
    cards.map((card) => card.label),
    ['今日成功', '今日失败', '今日成功率', '进行中/待签'],
  )
  assert.deepEqual(
    cards.map((card) => card.value),
    [5, 2, 71.4, 2],
  )
  assert.equal(cards[2].pill, stats.pill)
  assert.equal(cards[0].pill, undefined, 'only the rate card carries a pill')

  const blank = todayKpiCards(null)
  assert.deepEqual(
    blank.map((card) => card.value),
    [null, null, null, null],
    'no data means no invented numbers',
  )
  assert.deepEqual(
    blank.map((card) => card.pill ?? null),
    [null, null, null, null],
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

// ---------- 看板三图的纯逻辑 ----------

import {
  distributionConfig,
  prefConfig,
  prefSummary,
  trendConfig,
  trendTotals,
} from '../../src/features/dashboard/charts.ts'

const CHART_PALETTE = {
  success: '#10B981',
  danger: '#EF4444',
  warning: '#F59E0B',
  info: '#0EA5E9',
  primary: '#2563EB',
  text: '#1E293B',
  muted: '#64748B',
  grid: '#E4E8EF',
}

function statsReport() {
  return {
    days: [
      { day: '2026-10-01', pending: 0, running: 0, success: 3, failed: 1, skipped: 0 },
      { day: '2026-10-02', pending: 1, running: 0, success: 2, failed: 0, skipped: 1 },
    ],
    total: 4,
    by_status: { pending: 1, running: 0, success: 3, failed: 1, skipped: 1 },
    days_missing: ['2026-09-30'],
  }
}

test('trend totals add each day and count missing days separately', () => {
  const totals = trendTotals(statsReport())
  assert.deepEqual(totals, {
    success: 5,
    failed: 1,
    skipped: 1,
    pending: 1,
    running: 0,
    total: 8,
    missing: 1,
  })
})

test('the trend chart is null when every bucket is zero', () => {
  const empty = {
    days: [{ day: '2026-10-01', pending: 0, running: 0, success: 0, failed: 0, skipped: 0 }],
    total: 0,
    by_status: { pending: 0, running: 0, success: 0, failed: 0, skipped: 0 },
    days_missing: [],
  }
  assert.equal(trendConfig(undefined, CHART_PALETTE), null)
  assert.equal(trendConfig(empty, CHART_PALETTE), null)
})

test('the trend chart keeps three main series and adds in-flight states only when present', () => {
  const config = trendConfig(statsReport(), CHART_PALETTE)
  assert.deepEqual(config.data.labels, ['10-01', '10-02'])
  assert.deepEqual(
    config.data.datasets.map((dataset) => dataset.label),
    ['成功', '失败', '跳过', '待签'],
  )
  const settled = trendConfig(
    { ...statsReport(), by_status: { pending: 0, running: 0, success: 3, failed: 1, skipped: 1 } },
    CHART_PALETTE,
  )
  assert.equal(settled.data.datasets.length, 3)
  const running = trendConfig(
    { ...statsReport(), by_status: { pending: 0, running: 2, success: 3, failed: 1, skipped: 1 } },
    CHART_PALETTE,
  )
  assert.deepEqual(
    running.data.datasets.map((dataset) => dataset.label),
    ['成功', '失败', '跳过', '进行中'],
  )
})

test('the distribution chart drops zero buckets and refuses an all-zero window', () => {
  assert.equal(
    distributionConfig(
      { ...statsReport(), by_status: { pending: 0, running: 0, success: 0, failed: 0, skipped: 0 } },
      CHART_PALETTE,
    ),
    null,
  )
  const config = distributionConfig(statsReport(), CHART_PALETTE)
  assert.deepEqual(config.data.labels, ['成功', '失败', '跳过', '待签'])
  assert.deepEqual(config.data.datasets[0].data, [3, 1, 1, 1])
})

test('the preference chart pairs counts with per-slice capacity', () => {
  assert.equal(prefConfig(undefined, CHART_PALETTE), null)
  const report = {
    school_id: 'school-a',
    task_id: 'task-1',
    day: '2026-10-28',
    window_start: '2026-10-28T08:30:00+08:00',
    window_end: '2026-10-28T09:00:00+08:00',
    slots: [
      { offset_min: 0, label: '08:30', count: 2, cap: 5 },
      { offset_min: 5, label: '08:35', count: 0, cap: 5 },
    ],
  }
  const config = prefConfig(report, CHART_PALETTE)
  assert.deepEqual(config.data.labels, ['08:30', '08:35'])
  assert.equal(config.data.datasets.length, 2)
  assert.deepEqual(config.data.datasets[1].data, [5, 5])
  assert.deepEqual(prefSummary(report), { users: 2, hottest: { label: '08:30', count: 2 } })
})

test('the preference summary does not invent a hottest slot when nobody picked', () => {
  const report = {
    school_id: 'school-a',
    task_id: 'task-1',
    day: '2026-10-28',
    window_start: '2026-10-28T08:30:00+08:00',
    window_end: '2026-10-28T09:00:00+08:00',
    slots: [{ offset_min: 0, label: '08:30', count: 0, cap: 5 }],
  }
  assert.deepEqual(prefSummary(report), { users: 0, hottest: null })
})

test('the showcase crowding slots follow the selected window', async () => {
  const { showcasePrefStats, showcaseWindows } = await import('../../src/lib/showcase.ts')
  const day = '2026-10-28'
  const row = showcaseWindows(day).items.find((item) => item.task_id === 'task-daily')
  assert.ok(row)
  const report = showcasePrefStats({ day, schoolId: row.school_id, taskId: row.task_id })
  // 08:30–09:00 是 30 分钟：整 6 个五分钟槽。
  assert.equal(report.slots.length, 6)
  assert.equal(report.slots.at(-1).offset_min, 25)
  assert.equal(report.slots.at(-1).label, '08:55')
})
