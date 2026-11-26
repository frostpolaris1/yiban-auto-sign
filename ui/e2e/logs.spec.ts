import { test, expect, type Page } from '@playwright/test'
import {
  ENGINE_SNAPSHOT,
  RUN_TASKS,
  engineCalls,
  fakeSourceCloseCalls,
  fakeSourceClosed,
  fakeSourceCount,
  fakeSourceEmit,
  fakeSourceEnd,
  fakeSourceFail,
  fakeSourceHeartbeat,
  fakeSourceOpen,
  fakeSourceResync,
  fakeSourceSnapshot,
  installFakeEventSource,
  listTaskCalls,
  mockLogsApi,
  pauseCalls,
  schoolsCalls,
} from './support/fake-logs-api'

/**
 * 签到台账（`/logs`）的不变式（合并原 `run.spec.ts` 与 `engine.spec.ts`）。
 *
 * 1. 路由与重定向：`/logs` 默认落 `签到事件` 页签；`/run`（含查询串）与 `/engine`
 *    分别 302 到 `/logs?tab=events` 与 `/logs?tab=patrol`，地址栏落台账。
 * 2. 角色可见性：master/admin 从侧栏「签到台账」进页；user 无导航项，
 *    直达任一页签只见无权限卡片，且零任务请求、零引擎快照请求。
 * 3. 单 SSE 连接与按页签轮询：两页签共用一条 SSE 连接，切换页签不重建连接
 *    （`fakeSourceCount === 1`）；断线 10s 轮询只请求当前激活页签的数据源。
 * 4. 签到事件页签：今日轮次摘要五计数、按学校泳道、历史检索（业务日/状态/学校/关键词）、
 *    URL 写回与重载保持、CSV 导出（带筛选不含游标）与就地报错、行诊断抽屉（点击与键盘可达）。
 * 5. 运行巡检页签：四指标、进程时刻、在飞清单（租约倒计时每秒递减）、待重试列表
 *    （含 200 条截断提示）、最近事件流（容量 20、心跳不列、#id 续传、45s 看门狗、end 关闭）。
 * 6. 错误态与空态语义分离；全页不出现「队列」字样；390px 宽两页签均无横向溢出。
 */

async function openEventsTab(page: Page) {
  await expect(page.getByRole('heading', { name: '今日轮次摘要' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /任务时间线/ })).toBeVisible()
}

async function openPatrolTab(page: Page) {
  await expect(page.getByRole('heading', { name: '执行概览' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '在飞清单' })).toBeVisible()
}

test('invariant 1: /run and /engine redirect (302) to /logs?tab=events|patrol and preserve filters', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })

  await page.goto('/run?day=2026-10-30&state=failed&q=%E7%94%B2%E6%A0%A1')
  await openEventsTab(page)
  await expect(page).toHaveURL(/\/logs\?/)
  await expect(page).toHaveURL(/tab=events/)
  await expect(page).toHaveURL(/day=2026-10-30/)
  await expect(page).toHaveURL(/state=failed/)
  expect(listTaskCalls(api).length).toBeGreaterThanOrEqual(2)

  await page.goto('/engine')
  await openPatrolTab(page)
  await expect(page).toHaveURL(/\/logs\?tab=patrol$/)
  expect(engineCalls(api).length).toBeGreaterThanOrEqual(1)
})

test('invariant 1: master reaches /logs from the sidebar and switches tabs via deep link', async ({
  page,
}) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })

  await page.goto('/')
  await page
    .locator('.d-sidebar nav[aria-label="今日"]')
    .getByRole('link', { name: '签到台账', exact: true })
    .click()
  await expect(page).toHaveURL(/\/logs$/)
  await expect(page.locator('.crumbs')).toContainText('今日')
  await expect(page.locator('.crumbs .current')).toHaveText('签到台账')
  await openEventsTab(page)
  await expect(page.getByRole('tab', { name: '签到事件' })).toHaveAttribute('aria-selected', 'true')

  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)
  await expect(page.getByRole('tab', { name: '运行巡检' })).toHaveAttribute('aria-selected', 'true')
})

// ticket13：user 不再进管理壳——壳层一律 302 用户端 /app，不落无权限卡片。
test('invariant 1: the user role has no sidebar entry and is redirected with zero requests on either tab', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'user' })
  await page.goto('/')
  await expect(
    page.locator('.d-sidebar').getByRole('link', { name: '签到台账', exact: true }),
  ).toHaveCount(0)

  await page.goto('/logs?tab=events')
  await expect(page, 'user never lands the console logs page').toHaveURL('/app')
  await expect(page.getByText('无权访问')).toHaveCount(0)
  expect(listTaskCalls(api), 'a redirected user sends no task request').toHaveLength(0)

  await page.goto('/logs?tab=patrol')
  await expect(page).toHaveURL('/app')
  expect(engineCalls(api), 'a redirected user sends no engine request').toHaveLength(0)
})

test('invariant 1: the admin role reaches both tabs', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'admin' })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)

  await page.getByRole('tab', { name: '运行巡检' }).click()
  await expect(page).toHaveURL(/tab=patrol/)
  await openPatrolTab(page)
})

test('invariant 2: switching tabs keeps a single SSE connection and only polls the active tab when disconnected', async ({
  page,
}) => {
  await page.clock.install()
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })

  await page.goto('/logs?tab=events')
  await openEventsTab(page)
  await fakeSourceOpen(page)
  expect(await fakeSourceCount(page)).toBe(1)

  // 切到运行巡检再切回签到事件：SSE 连接数恒为 1，且巡检页签能看到同一连接收到的事件。
  await fakeSourceEmit(page, { type: 'task.concluded', id: '42' })
  await page.getByRole('tab', { name: '运行巡检' }).click()
  await openPatrolTab(page)
  expect(await fakeSourceCount(page), 'tab switch must not rebuild EventSource').toBe(1)
  const eventsCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '最近事件' }),
  })
  await expect(eventsCard.getByText('任务终态')).toBeVisible()
  await expect(eventsCard.getByText('#42')).toBeVisible()

  // 断线后在「运行巡检」页签推进 10s：只轮询 engine，不轮询 tasks。
  await fakeSourceFail(page)
  await expect(page.locator('.run-live-banner')).toBeVisible()
  const tasksAtPatrolDisconnect = listTaskCalls(api).length
  const engineAtPatrolDisconnect = engineCalls(api).length
  await page.clock.runFor(10_000)
  await expect.poll(() => engineCalls(api).length).toBeGreaterThan(engineAtPatrolDisconnect)
  expect(
    listTaskCalls(api).length,
    'disconnected poll on patrol tab must not query tasks',
  ).toBe(tasksAtPatrolDisconnect)

  // 切回「签到事件」页签推进 10s：只轮询 tasks，不轮询 engine；SSE 连接数仍为 1。
  await page.getByRole('tab', { name: '签到事件' }).click()
  await openEventsTab(page)
  expect(await fakeSourceCount(page)).toBe(1)
  const tasksAfterSwitch = listTaskCalls(api).length
  const engineAfterSwitch = engineCalls(api).length
  await page.clock.runFor(10_000)
  await expect.poll(() => listTaskCalls(api).length).toBeGreaterThan(tasksAfterSwitch)
  expect(
    engineCalls(api).length,
    'disconnected poll on events tab must not query engine',
  ).toBe(engineAfterSwitch)
})

// --- 签到事件页签（原 run.spec.ts 全量断言） ---------------------------------

test('events invariant 2: the summary counts and school lanes render', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)

  const summary = page.locator('dl', { hasText: '总任务' })
  await expect(summary).toBeVisible()
  await expect(summary).toContainText('5')
  await expect(summary).toContainText('1')

  const laneA = page.getByRole('region', { name: '泳道 合成学校甲' })
  await expect(laneA).toBeVisible()
  await expect(laneA).toContainText('138****8001')
  await expect(laneA).toContainText('138****8003')
  await expect(laneA.getByText('失败', { exact: true })).toBeVisible()
  const laneB = page.getByRole('region', { name: '泳道 合成学校乙' })
  await expect(laneB).toBeVisible()
  await expect(laneB).toContainText('138****8004')
  await expect(laneB.getByText('待签', { exact: true })).toBeVisible()
})

test('events invariant 3: an error renders an alert, never the empty copy', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, {
    role: 'master',
    listError: { status: 500, code: 'internal_error', message: '数据库不可用' },
  })
  await page.goto('/logs?tab=events')

  await expect(page.getByRole('alert').filter({ hasText: '数据库不可用' }).first()).toBeVisible()
  await expect(page.getByText('这一天没有匹配的任务')).toHaveCount(0)
  await expect(page.getByText('没有匹配的任务')).toHaveCount(0)
})

test('events invariant 3: an empty result renders the empty copy, never an alert', async ({
  page,
}) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master', tasks: [] })
  await page.goto('/logs?tab=events')
  await expect(page.getByText('这一天没有匹配的任务').first()).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('events invariant 4: filters survive a reload and are written back to the URL', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=events&day=2026-10-30&state=failed&q=%E7%94%B2%E6%A0%A1')
  await openEventsTab(page)

  await page.reload()
  await openEventsTab(page)
  await expect(page).toHaveURL(/tab=events/)
  await expect(page).toHaveURL(/day=2026-10-30/)
  await expect(page).toHaveURL(/state=failed/)
  await expect(page).toHaveURL(/q=/)

  const calls = listTaskCalls(api).filter((call) => call.params.day === '2026-10-30')
  expect(calls.length).toBeGreaterThan(0)
  const latest = calls[calls.length - 1]
  expect(latest.params.state).toBe('failed')
  expect(latest.params.q).toBe('甲校')

  await page.getByLabel('学校', { exact: true }).selectOption({ label: '合成学校乙' })
  await expect(page).toHaveURL(/school=school-beta/)
  await expect
    .poll(() => listTaskCalls(api).filter((call) => call.params.school_id === 'school-beta').length)
    .toBeGreaterThan(0)

  await page.getByLabel('状态筛选').selectOption('')
  await expect(page).not.toHaveURL(/state=failed/)
})

test('events invariant 5: a task event refreshes and a broken stream shows the banner', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)

  await fakeSourceOpen(page)
  expect(await fakeSourceCount(page)).toBe(1)
  const before = listTaskCalls(api).length
  const schoolsBefore = schoolsCalls(api).length

  await fakeSourceEmit(page, 'task.concluded')
  await expect
    .poll(() => listTaskCalls(api).length, { timeout: 5000 })
    .toBeGreaterThan(before)
  expect(schoolsCalls(api).length).toBe(schoolsBefore)

  await fakeSourceFail(page)
  const banner = page.locator('.run-live-banner')
  await expect(banner).toBeVisible()

  await page.getByRole('button', { name: '重连' }).click()
  expect(await fakeSourceCount(page)).toBe(2)
  await fakeSourceOpen(page)
  await expect(page.locator('.run-live-banner')).toHaveCount(0)
})

test('events invariant 5: a disconnected page polls every 10 seconds', async ({ page }) => {
  test.setTimeout(30_000)
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)

  await fakeSourceFail(page)
  await expect(page.locator('.run-live-banner')).toBeVisible()

  const baseline = listTaskCalls(api).length
  const schoolsBefore = schoolsCalls(api).length
  await page.waitForTimeout(5_000)
  expect(listTaskCalls(api).length).toBe(baseline)
  await expect
    .poll(() => listTaskCalls(api).length, { timeout: 15_000, intervals: [1_000, 5_000, 10_000] })
    .toBeGreaterThan(baseline)
  expect(schoolsCalls(api).length).toBe(schoolsBefore)
})

test('events invariant 5: a resync banner clears when the snapshot lands', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)

  const timeline = page.locator('section.card', {
    has: page.getByRole('heading', { name: /任务时间线/ }),
  })
  const banner = page.locator('.run-live-banner')
  const before = listTaskCalls(api).length

  await fakeSourceResync(page, 3)
  await expect(banner).toContainText('正在补发事件…')
  await expect(timeline.getByText('补发中', { exact: true })).toBeVisible()
  await expect.poll(() => listTaskCalls(api).length).toBeGreaterThan(before)

  await fakeSourceSnapshot(page)
  await expect(banner).toHaveCount(0)
  await expect(timeline.getByText('实时', { exact: true })).toBeVisible()
})

test('events invariant 6: a row opens diagnostics by click and by keyboard', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)

  const laneA = page.getByRole('region', { name: '泳道 合成学校甲' })
  const button = laneA.getByRole('button', { name: '诊断' }).first()
  await button.focus()
  await button.press('Enter')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('138****8001')
  await expect(dialog).toContainText('合成学校甲')
  await page.keyboard.press('Escape')

  await expect(button).toBeFocused()
  await button.press('Enter')
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Escape')
})

test('events invariant 6: a diagnostics failure shows an error, not an empty state', async ({
  page,
}) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, {
    role: 'master',
    diagnosticsError: { status: 500, code: 'internal_error', message: '诊断不可用' },
  })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)
  await page
    .getByRole('region', { name: '泳道 合成学校甲' })
    .getByRole('button', { name: '诊断' })
    .first()
    .click()
  await expect(page.getByRole('dialog').getByRole('alert')).toBeVisible()
})

test('events invariant 7: the page has no horizontal overflow at 390px', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)
  await page.getByRole('button', { name: '展开' }).click()
  await expect(page.getByRole('table')).toBeVisible()
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow, 'scrollWidth must not exceed the viewport').toBeLessThanOrEqual(0)
})

test('events invariant 4: the CSV export downloads with the current filters', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)
  await page.getByRole('button', { name: '展开' }).click()

  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 CSV' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('tasks-2026-10-31.csv')

  const exportCall = api.calls.find((call) => call.path === '/api/admin/tasks/export')
  expect(exportCall).toBeTruthy()
  expect(exportCall?.params.day).toBeDefined()
  expect(exportCall?.params.cursor).toBeUndefined()
})

test('events invariant 4: an export failure stays visible next to the control', async ({
  page,
}) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, {
    role: 'master',
    exportError: { status: 500, code: 'internal_error', message: '导出不可用' },
  })
  await page.goto('/logs?tab=events')
  await openEventsTab(page)
  await page.getByRole('button', { name: '展开' }).click()
  await page.getByRole('button', { name: '导出 CSV' }).click()
  await expect(page.getByRole('alert').filter({ hasText: '导出失败' })).toBeVisible()
})

test('events invariant 2: five tasks with two schools is the fixture baseline', async () => {
  expect(RUN_TASKS.filter((row) => row.school_id === 'school-a')).toHaveLength(3)
  expect(RUN_TASKS.filter((row) => row.school_id === 'school-b')).toHaveLength(2)
})

// --- 运行巡检页签（原 engine.spec.ts 全量断言） ------------------------------

test('patrol invariant 2: the four metrics and the inflight rows render', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const summary = page.locator('dl', { hasText: '待执行' })
  await expect(summary).toBeVisible()
  await expect(summary).toContainText('3')
  await expect(summary).toContainText('2')
  await expect(summary).toContainText('12')
  await expect(summary).toContainText('1')

  const inflight = page.locator('section.card', {
    has: page.getByRole('heading', { name: '在飞清单' }),
  })
  await expect(inflight).toContainText('138****8001')
  await expect(inflight).toContainText('合成学校甲')
  await expect(inflight).toContainText('138****8004')
  await expect(inflight).toContainText('1 分')
})

test('patrol invariant 2: an empty inflight renders its own empty copy', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, {
    role: 'master',
    snapshot: { ...ENGINE_SNAPSHOT, inflight: [], retrying: [] },
  })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  await expect(page.getByText('当前没有在飞任务。')).toBeVisible()
  await expect(page.getByText('没有待重试的任务。')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('patrol invariant 3: the lease remaining counts down while watching', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const cell = page.locator('.engine-lease').first()
  const before = await cell.textContent()
  expect(before).toMatch(/分|秒/)
  await expect
    .poll(async () => cell.textContent(), { timeout: 5000, intervals: [250] })
    .not.toBe(before)
})

test('patrol invariant 4: the retry list shows attempts, next run and result', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const retryCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '待重试' }),
  })
  await expect(retryCard).toContainText('138****8003')
  await expect(retryCard).toContainText('早签到')
  await expect(retryCard).toContainText('2 次')
  await expect(retryCard).toContainText('transient')
  await expect(retryCard).toContainText('晚自习签到')
  await expect(retryCard).toContainText('rejected')
  await expect(retryCard.getByRole('row')).toHaveCount(3)
})

test('patrol invariant 4: a truncated retry list says so', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, {
    role: 'master',
    snapshot: { ...ENGINE_SNAPSHOT, retrying_truncated: true },
  })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  await expect(page.getByText('待重试超过 200 条，仅显示最先到期的 200 条。')).toBeVisible()
})

test('patrol invariant 5: SSE business events land in the recent-events list', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const eventsCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '最近事件' }),
  })
  await expect(eventsCard.getByText('还没有收到业务事件。')).toBeVisible()

  await fakeSourceOpen(page)
  await fakeSourceEmit(page, 'task.concluded')
  await fakeSourceEmit(page, 'task.progress')
  await expect(eventsCard.getByText('任务终态')).toBeVisible()
  await expect(eventsCard.getByText('任务重排')).toBeVisible()
})

test('patrol invariant 5: a broken stream shows the banner and reconnects', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  await fakeSourceFail(page)
  const banner = page.locator('.run-live-banner')
  await expect(banner).toContainText('已停止实时')

  const before = await fakeSourceCount(page)
  await banner.getByRole('button', { name: '重连' }).click()
  await expect(banner).toHaveCount(0)
  expect(await fakeSourceCount(page)).toBeGreaterThan(before)
})

test('patrol invariant 5: the disconnected page falls back to polling', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  await fakeSourceFail(page)
  await expect(page.locator('.run-live-banner')).toBeVisible()
  const before = engineCalls(api).length
  await expect
    .poll(() => engineCalls(api).length, { timeout: 12_000, intervals: [1_000] })
    .toBeGreaterThan(before)
})

test('patrol invariant 5: an end frame closes the stream and stops refreshing', async ({
  page,
}) => {
  await page.clock.install()
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const baseline = engineCalls(api).length
  const banner = page.locator('.run-live-banner')
  const inflightCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '在飞清单' }),
  })
  const eventsCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '最近事件' }),
  })
  await fakeSourceEnd(page, '会话已失效')

  await expect(banner).toContainText('实时流已结束：会话已失效')
  await expect(banner.getByRole('button', { name: '重连' })).toBeVisible()
  expect(await fakeSourceClosed(page), 'end closes the client source').toBe(true)
  expect(
    await fakeSourceCloseCalls(page),
    'the hook calls close(), not only the frame',
  ).toBeGreaterThan(0)

  await expect.poll(() => engineCalls(api).length).toBeGreaterThan(baseline)
  const afterEnd = engineCalls(api).length
  const sourceCount = await fakeSourceCount(page)
  await page.clock.runFor(10_000)
  expect(engineCalls(api).length, 'an ended stream sends no poll').toBe(afterEnd)
  expect(await fakeSourceCount(page), 'an ended stream creates no source').toBe(sourceCount)

  const lateEvents = await eventsCard.locator('li').count()
  await fakeSourceEmit(page, { type: 'task.concluded', id: '9' })
  await page.clock.runFor(1_000)
  await expect(eventsCard.locator('li')).toHaveCount(lateEvents)
  await expect(eventsCard.getByText('#9')).toHaveCount(0)
  expect(engineCalls(api).length, 'a late event does not invalidate').toBe(afterEnd)

  await fakeSourceFail(page)
  await expect(banner).toContainText('实时流已结束：会话已失效')
  await expect(inflightCard.getByText('已结束', { exact: true })).toBeVisible()

  await expect(eventsCard.getByText('流结束')).toBeVisible()

  await banner.getByRole('button', { name: '重连' }).click()
  await expect(banner).toHaveCount(0)
  expect(await fakeSourceCount(page), 'reconnect after end builds a new source').toBe(
    sourceCount + 1,
  )
  await fakeSourceEnd(page)
  await expect(banner).toContainText('实时流已结束：服务器关闭连接')
})

test('patrol invariant 5: resync marks the gap and snapshot returns to live', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const inflightCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '在飞清单' }),
  })
  const eventsCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '最近事件' }),
  })
  const banner = page.locator('.run-live-banner')

  const baseline = engineCalls(api).length
  await fakeSourceResync(page, 2, '7')
  await expect(banner).toContainText('正在补发事件…')
  await expect(inflightCard.getByText('补发中', { exact: true })).toBeVisible()
  await expect(eventsCard.getByText('缺口重同步')).toBeVisible()
  await expect(eventsCard.getByText('#7')).toBeVisible()
  await expect.poll(() => engineCalls(api).length).toBeGreaterThan(baseline)
  const afterResync = engineCalls(api).length

  await fakeSourceSnapshot(page, { due_now: 3 }, '8')
  await expect(banner).toHaveCount(0)
  await expect(inflightCard.getByText('实时', { exact: true })).toBeVisible()
  await expect(eventsCard.getByText('前景快照')).toBeVisible()
  await expect(eventsCard.getByText('#8')).toBeVisible()
  await expect.poll(() => engineCalls(api).length).toBeGreaterThan(afterResync)
})

test('patrol invariant 5: a heartbeat pulls resyncing back to live', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const inflightCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '在飞清单' }),
  })
  const eventsCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '最近事件' }),
  })
  const banner = page.locator('.run-live-banner')

  await fakeSourceResync(page, 2, '7')
  await expect(banner).toContainText('正在补发事件…')
  await expect(inflightCard.getByText('补发中', { exact: true })).toBeVisible()

  await fakeSourceHeartbeat(page)
  await expect(banner).toHaveCount(0)
  await expect(inflightCard.getByText('实时', { exact: true })).toBeVisible()
  await expect(eventsCard.locator('li')).toHaveCount(1)

  await fakeSourceSnapshot(page, { due_now: 3 }, '8')
  await expect(eventsCard.getByText('前景快照')).toBeVisible()
  await expect(eventsCard.getByText('#8')).toBeVisible()
  await expect(eventsCard.locator('li'), 'heartbeat is never listed').toHaveCount(2)
})

test('patrol invariant 5: the heartbeat watchdog drops a silent stream to disconnected', async ({
  page,
}) => {
  await page.clock.install()
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const banner = page.locator('.run-live-banner')
  const inflightCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '在飞清单' }),
  })
  const eventsCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '最近事件' }),
  })

  expect(await fakeSourceCount(page), 'one live source').toBe(1)
  const now = await page.evaluate(() => Date.now())
  await page.clock.pauseAt(now + 100)
  const quietFrom = engineCalls(api).length
  await fakeSourceHeartbeat(page)
  await page.clock.runFor(44_999)
  await expect(banner, '44.999s is still inside the window').toHaveCount(0)
  expect(engineCalls(api).length, 'no poll while live').toBe(quietFrom)
  await page.clock.runFor(1)

  await expect(banner).toContainText('已停止实时')
  expect(await fakeSourceClosed(page), 'the watchdog keeps the source for self-healing').toBe(false)
  await expect(eventsCard.getByText('还没有收到业务事件。')).toBeVisible()

  const before = engineCalls(api).length
  await page.clock.runFor(10_000)
  await page.clock.runFor(10_000)
  await expect.poll(() => engineCalls(api).length).toBeGreaterThan(before)

  await fakeSourceHeartbeat(page)
  await expect(banner).toHaveCount(0)
  await expect(inflightCard.getByText('实时', { exact: true })).toBeVisible()
  await expect(eventsCard.getByText('还没有收到业务事件。')).toBeVisible()

  const recovered = engineCalls(api).length
  await page.clock.runFor(20_000)
  expect(engineCalls(api).length, 'recovery stops the poll').toBe(recovered)
  await expect(banner).toHaveCount(0)
})

test('patrol invariant 5: heartbeats inside the window keep the stream live', async ({ page }) => {
  await page.clock.install()
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const baseline = engineCalls(api).length
  await fakeSourceOpen(page)
  for (let index = 0; index < 4; index += 1) {
    await page.clock.runFor(15_000)
    await fakeSourceHeartbeat(page)
  }

  await expect(page.locator('.run-live-banner')).toHaveCount(0)
  expect(engineCalls(api).length, 'a heartbeat never invalidates the snapshot').toBe(baseline)
})

test('patrol invariant 5: a resumed event shows its id and reconnect starts a new source', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const eventsCard = page.locator('section.card', {
    has: page.getByRole('heading', { name: '最近事件' }),
  })
  await fakeSourceEmit(page, { type: 'task.concluded', id: '7' })
  await expect(eventsCard.getByText('任务终态')).toBeVisible()
  await expect(eventsCard.getByText('#7')).toBeVisible()

  await fakeSourceFail(page)
  const banner = page.locator('.run-live-banner')
  await expect(banner).toContainText('已停止实时')
  const sourceBefore = await fakeSourceCount(page)
  const callsBefore = engineCalls(api).length

  await banner.getByRole('button', { name: '重连' }).click()
  expect(await fakeSourceCount(page), 'reconnect builds a new source').toBe(sourceBefore + 1)
  await expect(banner).toHaveCount(0)
  await expect.poll(() => engineCalls(api).length).toBeGreaterThan(callsBefore)
})

test('patrol invariant 5: an error keeps the browser source for automatic resumption', async ({
  page,
}) => {
  await page.clock.install()
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const sourceBefore = await fakeSourceCount(page)
  await fakeSourceFail(page)
  await expect(page.locator('.run-live-banner')).toContainText('已停止实时')

  await page.clock.runFor(10_000)
  expect(await fakeSourceCount(page), 'onerror must not replace the source').toBe(sourceBefore)
  await expect(page.locator('.run-live-banner')).toBeVisible()
})

test('patrol invariant 6: an error renders an alert, never the empty copy', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, {
    role: 'master',
    snapshotError: { status: 500, code: 'internal_error', message: '数据库不可用' },
  })
  await page.goto('/logs?tab=patrol')

  await expect(page.getByRole('alert').filter({ hasText: '数据库不可用' })).toBeVisible()
  await expect(page.getByText('当前没有在飞任务。')).toHaveCount(0)
})

test('patrol invariant 7: the monitor never says queue and fits 390px', async ({ page }) => {
  await installFakeEventSource(page)
  await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const body = await page.locator('main').innerText()
  expect(body).not.toContain('队列')

  await page.setViewportSize({ width: 390, height: 844 })
  const narrow = page.locator('body')
  await expect(async () => {
    const scrollWidth = await narrow.evaluate((el) => el.scrollWidth)
    const clientWidth = await narrow.evaluate((el) => el.clientWidth)
    expect(scrollWidth).toBeLessThanOrEqual(clientWidth)
  }).toPass()
})


test('invariant 8: a master resumes the paused engine from the patrol header', async ({
  page,
}) => {
  const api = await mockLogsApi(page, {
    role: 'master',
    snapshot: { ...ENGINE_SNAPSHOT, paused: true },
  })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const banner = page.getByText('执行引擎已暂停')
  await expect(banner).toBeVisible()
  await page.getByRole('button', { name: '恢复引擎' }).click()
  const dialog = page.getByRole('dialog', { name: '恢复执行引擎' })
  await expect(dialog).toBeVisible()
  await dialog.getByRole('textbox', { name: '你的登录口令' }).fill('p@ssword')
  await dialog.getByRole('button', { name: '恢复引擎' }).click()

  await expect(dialog).toBeHidden()
  const pauses = pauseCalls(api)
  expect(pauses, 'one resume PUT per confirm').toHaveLength(1)
  expect(JSON.parse(pauses[0].body as string)).toEqual({
    paused: false,
    current_password: 'p@ssword',
  })
  await expect(page.getByText('执行引擎已暂停')).toBeHidden()
})


test('invariant 8: an admin cannot resume from the patrol header and sends nothing', async ({
  page,
}) => {
  const api = await mockLogsApi(page, {
    role: 'admin',
    snapshot: { ...ENGINE_SNAPSHOT, paused: true },
  })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  await expect(page.getByText('执行引擎已暂停')).toBeVisible()
  const resume = page.getByRole('button', { name: '恢复引擎' })
  await expect(resume).toBeDisabled()
  await expect(resume).toHaveAttribute('title', '仅主管理员可操作')
  await resume.click({ force: true })
  expect(pauseCalls(api), 'a disabled resume must not reach the server').toHaveLength(0)
})

test('invariant 8: an admin cannot pause an unpaused engine from the patrol header', async ({
  page,
}) => {
  const api = await mockLogsApi(page, { role: 'admin' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  const pause = page.getByRole('button', { name: '暂停引擎' })
  await expect(pause).toBeDisabled()
  await expect(pause).toHaveAttribute('title', '仅主管理员可操作')
  await pause.click({ force: true })
  expect(pauseCalls(api), 'a disabled pause must not reach the server').toHaveLength(0)
})

test('invariant 8: a master pauses from the patrol header and the banner appears', async ({
  page,
}) => {
  const api = await mockLogsApi(page, { role: 'master' })
  await page.goto('/logs?tab=patrol')
  await openPatrolTab(page)

  await page.getByRole('button', { name: '暂停引擎' }).click()
  const dialog = page.getByRole('dialog', { name: '暂停执行引擎' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('暂停与恢复均仅主管理员')
  await dialog.getByRole('textbox', { name: '你的登录口令' }).fill('p@ssword')
  await dialog.getByRole('button', { name: '暂停引擎' }).click()

  const pauses = pauseCalls(api)
  expect(pauses, 'one pause PUT per confirm').toHaveLength(1)
  expect(JSON.parse(pauses[0].body as string)).toEqual({
    paused: true,
    current_password: 'p@ssword',
  })
  await expect(page.getByText('执行引擎已暂停')).toBeVisible()
})
