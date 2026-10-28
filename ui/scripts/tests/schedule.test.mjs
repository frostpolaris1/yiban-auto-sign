import assert from 'node:assert/strict'
import test from 'node:test'

import { NAV_LEAVES, visibleNav } from '../../src/components/shell/nav.ts'
import { ApiError } from '../../src/lib/api/client.ts'
import { schedulePreviewSearch, scheduleWindowsSearch } from '../../src/lib/api/schedule.ts'
import { dayOf, formatClock, isoToShanghaiInput, parseDay, shanghaiInputToRfc3339 } from '../../src/lib/format.ts'
import { SCHEDULE_VIEW_ROLES } from '../../src/lib/roles.ts'
import {
  enabledLabel,
  enabledTone,
  SOURCE_LABELS,
  sourceLabel,
} from '../../src/features/schedule/display.ts'
import {
  OVERRIDE_REASON_MAX,
  TIME_PREF_MAX,
  TIME_PREF_STEP,
  buildOverrideBody,
  buildPlanBody,
  histogramBars,
  overrideDraftError,
  overrideDraftFromPlan,
  scheduleErrorText,
  timePrefErrorText,
  timePrefLabel,
  timePrefOffsetError,
  timePrefOptions,
} from '../../src/features/schedule/drafts.ts'

/**
 * 时间片域纯逻辑的不变式。
 *
 * 查询串：day 必带；refresh 只在重新发现时出现。
 * 日期：只认 YYYY-MM-DD 且必须是真实日历日；上海时区固定 +08:00。
 * 直方图：按切片号数字升序；柱高是 count 与最大值的比例。
 * 覆盖草稿：reason 1..200；end 必须严格晚于 start；请求体只含契约六字段。
 * 时间偏好：偏移是 5 的倍数且在 0..120；请求体只含 offset_min。
 * 错误文案：409 停用、429 冷却、403 未开放各有定稿。
 */

const DAY = '2026-10-28'

const PLAN = {
  day: DAY,
  enabled: true,
  source: 'platform',
  window_start: '2026-10-28T00:10:00Z',
  window_end: '2026-10-28T01:30:00Z',
  version: 0,
  capacity: 40,
  demand: 12,
  shortfall: 0,
  histogram: { '0': 3 },
  rows: [],
}

test('the windows query carries day and only adds refresh on demand', () => {
  assert.equal(scheduleWindowsSearch({ day: DAY }), `?day=${DAY}`)
  assert.equal(scheduleWindowsSearch({ day: DAY, refresh: true }), `?day=${DAY}&refresh=true`)
  assert.equal(scheduleWindowsSearch({ day: DAY, refresh: false }), `?day=${DAY}`)
})

test('the preview query carries the three row coordinates and escapes them', () => {
  assert.equal(
    schedulePreviewSearch({ day: DAY, schoolId: 'school-alpha', taskId: 'task-daily' }),
    `?day=${DAY}&school_id=school-alpha&task_id=task-daily`,
  )
  assert.match(
    schedulePreviewSearch({ day: DAY, schoolId: 'a b', taskId: 't&x' }),
    /school_id=a\+b&task_id=t%26x$/,
  )
})

test('day parsing accepts real calendar days only', () => {
  assert.equal(parseDay(DAY), DAY)
  assert.equal(parseDay('2026-02-28'), '2026-02-28')
  assert.equal(parseDay('2026-02-30'), null)
  assert.equal(parseDay('2026-13-01'), null)
  assert.equal(parseDay('2026-1-2'), null)
  assert.equal(parseDay(''), null)
  assert.equal(parseDay(undefined), null)
  assert.equal(parseDay(20261028), null)
})

test('the default day uses the Shanghai calendar, not the machine clock', () => {
  // 16:30Z 已是上海次日 00:30；15:59Z 仍是上海当日 23:59。
  assert.equal(dayOf(new Date('2026-10-27T16:30:00Z')), '2026-10-28')
  assert.equal(dayOf(new Date('2026-10-27T15:59:00Z')), '2026-10-27')
})

test('clock and datetime-local helpers use the fixed Shanghai offset', () => {
  assert.equal(formatClock('2026-10-28T00:10:00Z'), '08:10')
  assert.equal(formatClock(null), '—')
  assert.equal(formatClock('not-a-date'), 'not-a-date')
  assert.equal(isoToShanghaiInput('2026-10-28T00:10:00Z'), '2026-10-28T08:10')
  assert.equal(shanghaiInputToRfc3339('2026-10-28T08:30'), '2026-10-28T08:30:00+08:00')
  assert.throws(() => shanghaiInputToRfc3339('2026-10-28'), /时间格式/)
})

test('histogram bars sort by slice number and scale to the peak', () => {
  const bars = histogramBars({ '10': 2, '2': 5, '0': 1 })
  assert.deepEqual(
    bars.map((bar) => bar.slice),
    [0, 2, 10],
  )
  assert.deepEqual(
    bars.map((bar) => bar.heightPct),
    [20, 100, 40],
  )
  assert.deepEqual(histogramBars({}), [])
  assert.deepEqual(histogramBars({ '0': 0 }), [{ slice: 0, count: 0, heightPct: 0 }])
  assert.deepEqual(histogramBars({ x: 3 }), [])
})

test('an override draft needs a reason and an end after the start', () => {
  const base = { start: '2026-10-28T08:00', end: '2026-10-28T09:00', reason: '合成调整' }
  assert.equal(overrideDraftError(base), null)
  assert.equal(overrideDraftError({ ...base, start: '' }), '开始时间必填')
  assert.equal(overrideDraftError({ ...base, end: '' }), '结束时间必填')
  assert.equal(overrideDraftError({ ...base, end: base.start }), '结束时间必须晚于开始时间')
  assert.equal(overrideDraftError({ ...base, end: '2026-10-28T07:59' }), '结束时间必须晚于开始时间')
  // 非法日历日与时分越界必须在草稿层报错：buildOverrideBody 不渲染期抛错。
  assert.equal(
    overrideDraftError({ ...base, start: '2026-02-30T08:00' }),
    '时间格式必须是 YYYY-MM-DDTHH:mm',
  )
  assert.equal(
    overrideDraftError({ ...base, end: '2026-03-01T25:00' }),
    '时间格式必须是 YYYY-MM-DDTHH:mm',
  )
  assert.equal(overrideDraftError({ ...base, reason: '   ' }), '修改原因必填')
  assert.equal(
    overrideDraftError({ ...base, reason: '长'.repeat(OVERRIDE_REASON_MAX + 1) }),
    '修改原因最多 200 字',
  )
  assert.equal(OVERRIDE_REASON_MAX, 200)
})

test('the override body carries exactly the six contract fields', () => {
  const body = buildOverrideBody({
    day: DAY,
    schoolId: 'school-alpha',
    taskId: 'task-daily',
    draft: { start: '2026-10-28T08:30', end: '2026-10-28T09:20', reason: ' 合成调整 ' },
  })
  assert.deepEqual(body, {
    day: DAY,
    school_id: 'school-alpha',
    task_id: 'task-daily',
    start: '2026-10-28T08:30:00+08:00',
    end: '2026-10-28T09:20:00+08:00',
    reason: '合成调整',
  })
  assert.deepEqual(Object.keys(body).sort(), ['day', 'end', 'reason', 'school_id', 'start', 'task_id'])
  assert.throws(
    () =>
      buildOverrideBody({
        day: DAY,
        schoolId: 'school-alpha',
        taskId: 'task-daily',
        draft: { start: '2026-10-28T09:00', end: '2026-10-28T09:00', reason: '合成调整' },
      }),
    /结束时间必须晚于开始时间/,
  )
})

test('the override form prefills the effective window in Shanghai wall time', () => {
  assert.deepEqual(overrideDraftFromPlan(PLAN), {
    start: '2026-10-28T08:10',
    end: '2026-10-28T09:30',
    reason: '',
  })
})

test('the plan body carries exactly the three contract fields', () => {
  const body = buildPlanBody({ day: DAY, schoolId: 'school-alpha', taskId: 'task-daily' })
  assert.deepEqual(body, { day: DAY, school_id: 'school-alpha', task_id: 'task-daily' })
  assert.equal(Object.keys(body).length, 3)
})

test('time preference options step by five from zero to the cap', () => {
  const options = timePrefOptions()
  assert.equal(TIME_PREF_STEP, 5)
  assert.equal(TIME_PREF_MAX, 120)
  assert.equal(options.length, 25)
  assert.deepEqual(options[0], { value: '0', label: '窗口开始 + 0 分钟' })
  assert.deepEqual(options[12], { value: '60', label: '窗口开始 + 60 分钟' })
  assert.deepEqual(options.at(-1), { value: '120', label: '窗口开始 + 120 分钟' })
})

test('time preference labels read as offset from the window start', () => {
  assert.equal(timePrefLabel(null), '未设置')
  assert.equal(timePrefLabel(0), '窗口开始 + 0 分钟')
  assert.equal(timePrefLabel(15), '窗口开始 + 15 分钟')
})

test('the offset value must be an integer multiple of five in range', () => {
  for (const value of ['0', '5', '15', '120']) {
    assert.equal(timePrefOffsetError(value), null, value)
  }
  assert.match(timePrefOffsetError(''), /必填/)
  assert.match(timePrefOffsetError('abc'), /整数/)
  assert.match(timePrefOffsetError('7'), /5 的倍数/)
  assert.match(timePrefOffsetError('-5'), /0–120/)
  assert.match(timePrefOffsetError('125'), /0–120/)
})

test('error text maps the pinned HTTP failures to plain words', () => {
  assert.equal(
    timePrefErrorText(new ApiError(429, { code: 'rate_limited', message: 'too frequent' })),
    '修改太频繁，请稍后再试',
  )
  assert.equal(
    timePrefErrorText(new ApiError(403, { code: 'forbidden', message: 'disabled' })),
    '此部署未开放时间偏好',
  )
  assert.equal(
    timePrefErrorText(new ApiError(500, { code: 'internal_error', message: '坏了' })),
    'internal_error · 坏了',
  )
  assert.equal(
    scheduleErrorText(new ApiError(409, { code: 'conflict', message: 'task disabled' })),
    '任务已停用',
  )
  assert.equal(
    scheduleErrorText(new ApiError(403, { code: 'forbidden', message: 'nope' })),
    '当前角色无权执行此操作',
  )
})

test('schedule labels come from the contract enums', () => {
  assert.deepEqual(SOURCE_LABELS, {
    platform: '平台',
    cache: '缓存',
    fallback: '回退',
    override: '覆盖',
  })
  assert.equal(sourceLabel('platform'), '平台')
  assert.equal(sourceLabel('override'), '覆盖')
  assert.equal(sourceLabel(null), '—')
  assert.equal(sourceLabel('new-source'), 'new-source', 'contract drift stays visible')
  assert.equal(enabledLabel(true), '已启用')
  assert.equal(enabledLabel(false), '已停用')
  assert.equal(enabledTone(true), 'success')
  assert.equal(enabledTone(false), 'warning')
})

function leaves(role) {
  return visibleNav(role).flatMap((section) =>
    section.items.flatMap((item) => ('children' in item ? item.children : [item])),
  )
}

test('the schedule entry is visible to managers only', () => {
  assert.deepEqual([...SCHEDULE_VIEW_ROLES], ['master', 'admin'])
  for (const role of ['master', 'admin']) {
    assert(
      leaves(role).some((leaf) => leaf.to === '/schedule'),
      `${role} sees the schedule entry`,
    )
  }
  assert(
    !leaves('user').some((leaf) => leaf.to === '/schedule'),
    'user has no schedule entry',
  )

  const leaf = NAV_LEAVES.find((item) => item.to === '/schedule')
  assert(leaf, 'the route has a nav leaf')
  assert.deepEqual([...(leaf.roles ?? [])], ['master', 'admin'])
  assert.deepEqual(leaf.crumbs, ['工作区', '时间片'])
})
