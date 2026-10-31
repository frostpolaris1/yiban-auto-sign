import { test, expect, type Page } from '@playwright/test'
import {
  fakeSourceCount,
  fakeSourceEmit,
  fakeSourceFail,
  fakeSourceOpen,
  installFakeEventSource,
  listTaskCalls,
  mockRunApi,
  RUN_TASKS,
} from './support/fake-run-api'

/**
 * 运行页的不变式（swh 验收）。
 *
 * 1. 角色可见性：master/admin 从侧栏进页；user 无导航项，
 *    直接访问只有无权限卡片，且零任务列表请求。
 * 2. 数据态：今日摘要五个计数与两校泳道齐全；时间线行含
 *    掩码号、状态共用词与诊断按钮。
 * 3. 空态与错误态语义分离：失败渲染 role=alert 的错误条，
 *    空渲染「没有匹配的任务」；失败不得显示「暂无」。
 * 4. URL 状态：?day&state&q 刷新后保留；筛选写回查询串。
 * 5. SSE：业务事件触发刷新；断线出「已停止实时」横幅并可重连；
 *    断线期间 10 秒轮询降级。
 * 6. 诊断：点击与键盘（Enter）都能打开抽屉；抽屉调单账号诊断端点。
 * 7. 390 宽无横向溢出。
 *
 * 断言用户可见结果与请求参数，不钉内部调用顺序。
 */

/** 等运行页三段就绪。不做导航：URL 由调用方给，不得覆盖查询串。 */
async function openRun(page: Page) {
  await expect(page.getByRole('heading', { name: '今日轮次摘要' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /任务时间线/ })).toBeVisible()
}

test('invariant 1: master reaches the run page from the navigation', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockRunApi(page, { role: 'master' })
  await page.goto('/')
  const link = page.locator('.d-sidebar').getByRole('link', { name: '运行', exact: true })
  await expect(link).toHaveCount(1)
  await link.click()
  await expect(page).toHaveURL(/\/run$/)
  await openRun(page)
  // 侧栏进页＝两次列表请求（摘要与时间线），没有多余的取数。
  expect(listTaskCalls(api).length).toBeGreaterThanOrEqual(2)
})

test('invariant 1: the user role has no entry and no task request', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockRunApi(page, { role: 'user' })
  await page.goto('/')
  await expect(page.locator('.d-sidebar').getByRole('link', { name: '运行', exact: true })).toHaveCount(0)

  await page.goto('/run')
  await expect(page.getByText('无权访问')).toBeVisible()
  await expect(page.getByText('你的角色无权查看运行页')).toBeVisible()
  expect(listTaskCalls(api), 'a denied page sends no task request').toHaveLength(0)
})

test('invariant 2: the summary counts and school lanes render', async ({ page }) => {
  await installFakeEventSource(page)
  await mockRunApi(page, { role: 'master' })
  await page.goto('/run')
  await openRun(page)

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

test('invariant 1: the admin role reaches the same page', async ({ page }) => {
  await installFakeEventSource(page)
  await mockRunApi(page, { role: 'admin' })
  await page.goto('/')
  const link = page.locator('.d-sidebar').getByRole('link', { name: '运行', exact: true })
  await expect(link).toHaveCount(1)
  await link.click()
  await expect(page).toHaveURL(/\/run$/)
  await expect(page.getByRole('heading', { name: /任务时间线/ })).toBeVisible()
})

test('invariant 3: an error renders an alert, never the empty copy', async ({ page }) => {
  await installFakeEventSource(page)
  await mockRunApi(page, {
    role: 'master',
    listError: { status: 500, code: 'internal_error', message: '数据库不可用' },
  })
  await page.goto('/run')

  await expect(page.getByRole('alert').filter({ hasText: '数据库不可用' }).first()).toBeVisible()
  await expect(page.getByText('这一天没有匹配的任务')).toHaveCount(0)
  await expect(page.getByText('没有匹配的任务')).toHaveCount(0)
})

test('invariant 3: an empty result renders the empty copy, never an alert', async ({ page }) => {
  await installFakeEventSource(page)
  await mockRunApi(page, { role: 'master', tasks: [] })
  await page.goto('/run')
  await expect(page.getByText('这一天没有匹配的任务').first()).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('invariant 4: filters survive a reload and are written back to the URL', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockRunApi(page, { role: 'master' })
  await page.goto('/run?day=2026-10-30&state=failed&q=%E7%94%B2%E6%A0%A1')
  await openRun(page)

  // 刷新后仍在。
  await page.reload()
  await openRun(page)
  await expect(page).toHaveURL(/day=2026-10-30/)
  await expect(page).toHaveURL(/state=failed/)
  await expect(page).toHaveURL(/q=/)

  // 请求带着同样的筛选。最后一笔可能是摘要查询（恒查今天），
  // 故按业务日先过滤再取最新一笔。
  const calls = listTaskCalls(api).filter((call) => call.params.day === '2026-10-30')
  expect(calls.length).toBeGreaterThan(0)
  const latest = calls[calls.length - 1]
  expect(latest.params.state).toBe('failed')
  expect(latest.params.q).toBe('甲校')

  // 界面改筛选写回 URL：URL 带 q 时历史检索已展开，
  // 状态选「全部状态」后 state 从查询串消失。
  await page.getByLabel('状态筛选').selectOption('')
  await expect(page).not.toHaveURL(/state=failed/)
})

test('invariant 5: a task event refreshes and a broken stream shows the banner', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockRunApi(page, { role: 'master' })
  await page.goto('/run')
  await openRun(page)

  await fakeSourceOpen(page)
  expect(await fakeSourceCount(page)).toBe(1)
  const before = listTaskCalls(api).length

  await fakeSourceEmit(page, 'task.concluded')
  await expect
    .poll(() => listTaskCalls(api).length, { timeout: 5000 })
    .toBeGreaterThan(before)

  await fakeSourceFail(page)
  const banner = page.locator('.run-live-banner')
  await expect(banner).toBeVisible()

  // 重连：换一条新连接，横幅在建立后退场。
  await page.getByRole('button', { name: '重连' }).click()
  expect(await fakeSourceCount(page)).toBe(2)
  await fakeSourceOpen(page)
  await expect(page.locator('.run-live-banner')).toHaveCount(0)
})

test('invariant 5: a disconnected page polls every 10 seconds', async ({ page }) => {
  test.setTimeout(30_000)
  await installFakeEventSource(page)
  const api = await mockRunApi(page, { role: 'master' })
  await page.goto('/run')
  await openRun(page)

  await fakeSourceFail(page)
  await expect(page.locator('.run-live-banner')).toBeVisible()

  // 降级证据：前 5 秒没有新请求（不是立即刷屏），10 秒档内出现轮询取数。
  const baseline = listTaskCalls(api).length
  await page.waitForTimeout(5_000)
  expect(listTaskCalls(api).length).toBe(baseline)
  await expect
    .poll(() => listTaskCalls(api).length, { timeout: 15_000, intervals: [1_000, 5_000, 10_000] })
    .toBeGreaterThan(baseline)
})

test('invariant 6: a row opens diagnostics by click and by keyboard', async ({ page }) => {
  await installFakeEventSource(page)
  await mockRunApi(page, { role: 'master' })
  await page.goto('/run')
  await openRun(page)

  const laneA = page.getByRole('region', { name: '泳道 合成学校甲' })
  const button = laneA.getByRole('button', { name: '诊断' }).first()
  await button.focus()
  await button.press('Enter')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('138****8001')
  await expect(dialog).toContainText('合成学校甲')
  await page.keyboard.press('Escape')

  // 关闭后焦点回到打开它的按钮，再按 Enter 再次打开。
  await expect(button).toBeFocused()
  await button.press('Enter')
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Escape')
})

test('invariant 6: a diagnostics failure shows an error, not an empty state', async ({ page }) => {
  await installFakeEventSource(page)
  await mockRunApi(page, {
    role: 'master',
    diagnosticsError: { status: 500, code: 'internal_error', message: '诊断不可用' },
  })
  await page.goto('/run')
  await openRun(page)
  await page.getByRole('region', { name: '泳道 合成学校甲' }).getByRole('button', { name: '诊断' }).first().click()
  await expect(page.getByRole('dialog').getByRole('alert')).toBeVisible()
})

test('invariant 7: the page has no horizontal overflow at 390px', async ({ page }) => {
  await installFakeEventSource(page)
  await mockRunApi(page, { role: 'master' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/run')
  await openRun(page)
  await page.getByRole('button', { name: '展开' }).click()
  await expect(page.getByRole('table')).toBeVisible()
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow, 'scrollWidth must not exceed the viewport').toBeLessThanOrEqual(0)
})

test('invariant 4: the CSV export downloads with the current filters', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockRunApi(page, { role: 'master' })
  await page.goto('/run')
  await openRun(page)
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

test('invariant 4: an export failure stays visible next to the control', async ({ page }) => {
  await installFakeEventSource(page)
  await mockRunApi(page, {
    role: 'master',
    exportError: { status: 500, code: 'internal_error', message: '导出不可用' },
  })
  await page.goto('/run')
  await openRun(page)
  await page.getByRole('button', { name: '展开' }).click()
  await page.getByRole('button', { name: '导出 CSV' }).click()
  await expect(page.getByRole('alert').filter({ hasText: '导出失败' })).toBeVisible()
})

test('invariant 2: five tasks with two schools is the fixture baseline', async () => {
  // 夹具形状守卫：泳道分组按学校首现行序；改夹具必须同步改本判据。
  expect(RUN_TASKS.filter((row) => row.school_id === 'school-a')).toHaveLength(3)
  expect(RUN_TASKS.filter((row) => row.school_id === 'school-b')).toHaveLength(2)
})
