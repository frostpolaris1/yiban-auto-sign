import { test, expect, type Page } from '@playwright/test'
import { visibleText } from './support/visible-text'
import {
  type DashboardApiCall,
  capacityCalls,
  diagnosticsCalls,
  lastCapacityCall,
  mockDashboardApi,
  signStatusCalls,
  windowsCalls,
} from './support/fake-dashboard-api'

/**
 * 看板的七条不变式。
 *
 * 1. 角色取数隔离：user 零窗口与容量请求；master/admin 零签到状态与诊断请求。
 * 2. KPI 由条目派生，四项计数与列表行数一致；空账号全零。
 * 3. 轮询：30 秒后自动再取一次；页面隐藏时不轮询。
 * 4. 过期标记：stale=true 的行带「数据已过期」，全 false 时一处也没有。
 * 5. 容量体检：默认选第一可用任务并即发请求；切换选择器触发新请求；
 *    shortfall>0 用 danger 徽章。
 * 6. 空态与错误态：无账号两卡空态；空窗口说「今日无可用窗口」；
 *    单卡失败只挂该卡，另一卡照常。
 * 7. 展示模式零请求：`e2e/showcase.spec.ts`。
 *
 * 断言用户可见结果与请求参数，不钉组件内部结构。
 */

function kpiCard(page: Page, label: string) {
  return page.locator('.kpi-card').filter({ hasText: label })
}

function signTable(page: Page) {
  return page.getByRole('table', { name: '今日签到状态' })
}

function diagnosticsTable(page: Page) {
  return page.getByRole('table', { name: '签到诊断' })
}

function capacityCard(page: Page) {
  return page.locator('.card').filter({ hasText: '容量体检' })
}

async function openDashboard(page: Page) {
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('签到运行')
}

/** 数字由滚动组件渲染：读用户可见文本，不读 DOM 里的全部字形。 */
async function expectKpi(page: Page, label: string, value: string) {
  await expect.poll(() => visibleText(kpiCard(page, label).locator('.kpi-value'))).toBe(value)
}

function capacityParams(api: DashboardApiCall | undefined, name: string): string | undefined {
  return api?.params[name]
}

test('invariant 1: the user dashboard sends no window or capacity request', async ({ page }) => {
  const api = await mockDashboardApi(page, { role: 'user' })
  await openDashboard(page)
  await expect(signTable(page)).toBeVisible()
  await expect(diagnosticsTable(page)).toBeVisible()

  expect(windowsCalls(api), 'a user never reads the window list').toHaveLength(0)
  expect(capacityCalls(api), 'a user never reads capacity').toHaveLength(0)
  await expect(page.getByText('容量体检')).toHaveCount(0)
})

test('invariant 1: a manager sends no sign-status or diagnostics request', async ({ page }) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)
  await expect(capacityCard(page)).toBeVisible()

  expect(signStatusCalls(api), 'a manager never polls sign status').toHaveLength(0)
  expect(diagnosticsCalls(api), 'a manager never reads diagnostics').toHaveLength(0)
  await expect(signTable(page)).toHaveCount(0)
  await expect(diagnosticsTable(page)).toHaveCount(0)
})

test('invariant 2: the four KPI counts match the rows', async ({ page }) => {
  const items = [
    { account_id: 'acc-0001', state: 'success', run_at: null, stale: false },
    { account_id: 'acc-0002', state: 'success', run_at: null, stale: false },
    { account_id: 'acc-0003', state: 'pending', run_at: null, stale: false },
    { account_id: 'acc-0004', state: 'running', run_at: null, stale: false },
    { account_id: 'acc-0005', state: 'failed', run_at: null, stale: false },
    { account_id: 'acc-0006', state: 'skipped', run_at: null, stale: false },
    { account_id: 'acc-0007', state: null, run_at: null, stale: false },
  ]
  await mockDashboardApi(page, { role: 'user', signStatusItems: items })
  await openDashboard(page)

  await expectKpi(page, '账号总数', '7个')
  await expectKpi(page, '已签', '2个')
  await expectKpi(page, '进行中/待签', '2个')
  await expectKpi(page, '异常', '2个')
  await expect(signTable(page).locator('tbody tr')).toHaveCount(7)
})

test('invariant 2: no accounts means zero KPI and two empty cards', async ({ page }) => {
  await mockDashboardApi(page, { role: 'user', signStatusItems: [], diagnosticsItems: [] })
  await openDashboard(page)

  await expectKpi(page, '账号总数', '0个')
  await expectKpi(page, '已签', '0个')
  await expectKpi(page, '进行中/待签', '0个')
  await expectKpi(page, '异常', '0个')
  await expect(page.getByText('今日暂无签到任务')).toHaveCount(2)
  await expect(signTable(page).locator('tbody tr')).toHaveCount(1)
})

test('invariant 3: sign status polls again after thirty seconds', async ({ page }) => {
  await page.clock.install()
  const api = await mockDashboardApi(page, { role: 'user' })
  await openDashboard(page)
  await expect(signTable(page).locator('tbody tr')).toHaveCount(4)

  const base = signStatusCalls(api).length
  expect(base, 'one read when the card mounts').toBe(1)

  await page.clock.runFor(25_000)
  expect(signStatusCalls(api).length, 'no poll before the interval elapses').toBe(base)

  await page.clock.runFor(6_000)
  await expect.poll(() => signStatusCalls(api).length).toBe(base + 1)
})

test('invariant 3: a hidden page does not poll', async ({ page }) => {
  await page.clock.install()
  const api = await mockDashboardApi(page, { role: 'user' })
  await openDashboard(page)
  await expect(signTable(page).locator('tbody tr')).toHaveCount(4)

  const base = signStatusCalls(api).length
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    window.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(65_000)
  expect(signStatusCalls(api).length, 'a hidden page sends no poll').toBe(base)

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    window.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(31_000)
  await expect.poll(() => signStatusCalls(api).length).toBe(base + 1)
})

test('invariant 4: a stale row carries the expiry badge', async ({ page }) => {
  await mockDashboardApi(page, {
    role: 'user',
    signStatusItems: [
      { account_id: 'acc-0001', state: 'success', run_at: null, stale: true },
      { account_id: 'acc-0002', state: 'pending', run_at: null, stale: false },
    ],
  })
  await openDashboard(page)

  const rows = signTable(page).locator('tbody tr')
  await expect(rows.nth(0)).toContainText('数据已过期')
  await expect(rows.nth(1)).not.toContainText('数据已过期')
  await expect(signTable(page).getByText('数据已过期')).toHaveCount(1)
})

test('invariant 4: fresh rows carry no expiry badge', async ({ page }) => {
  await mockDashboardApi(page, { role: 'user' })
  await openDashboard(page)
  await expect(signTable(page).locator('tbody tr')).toHaveCount(4)
  await expect(signTable(page).getByText('数据已过期')).toHaveCount(0)
})

test('invariant 5: capacity defaults to the first usable task and follows the picker', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  const select = page.getByLabel('任务')
  await expect(select).toHaveValue('school-alpha:task-daily')
  await expect.poll(() => capacityParams(lastCapacityCall(api), 'school_id')).toBe('school-alpha')
  expect(capacityParams(lastCapacityCall(api), 'task_id')).toBe('task-daily')
  // 请求日按渲染时计算；只钉形状，避免跨 00:00 的假红。
  expect(capacityParams(lastCapacityCall(api), 'day'), 'the day rides the request').toMatch(
    /^\d{4}-\d{2}-\d{2}$/,
  )

  const options = select.locator('option')
  await expect(options).toHaveCount(2)
  await expect(options.nth(0)).toHaveText('合成学校甲 · 每日签到')
  await expect(options.nth(1)).toHaveText('合成学校乙 · 夜间签到')

  const card = capacityCard(page)
  const gap = card.locator('.app-metric').filter({ hasText: '缺口' })
  await expect(gap.locator('.badge')).toHaveClass(/success/)
  await expect.poll(() => visibleText(gap.locator('.badge'))).toBe('0')
  await expect(card).toContainText('08:30–09:00')
  await expect(card.locator('.badge').filter({ hasText: '平台' })).toBeVisible()

  await select.selectOption('school-beta:task-night')
  await expect.poll(() => capacityParams(lastCapacityCall(api), 'school_id')).toBe('school-beta')
  expect(capacityParams(lastCapacityCall(api), 'task_id')).toBe('task-night')
  expect(capacityParams(lastCapacityCall(api), 'day')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  await expect.poll(() => visibleText(gap.locator('.badge'))).toBe('6')
  await expect(gap.locator('.badge')).toHaveClass(/danger/)
})

test('invariant 6: an empty window list says so and sends no capacity request', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'admin', windows: [] })
  await openDashboard(page)

  await expect(page.getByText('今日无可用窗口')).toBeVisible()
  await expectKpi(page, '任务总数', '0项')
  expect(capacityCalls(api), 'no task means no capacity request').toHaveLength(0)
})

test('invariant 6: a failing sign-status read leaves the diagnostics card usable', async ({
  page,
}) => {
  await mockDashboardApi(page, {
    role: 'user',
    signStatusError: {
      status: 500,
      code: 'internal_error',
      message: '签到状态暂不可用',
      once: true,
    },
  })
  await openDashboard(page)

  const card = page.locator('.card').filter({ hasText: '签到状态' })
  await expect(card).toContainText('签到状态暂不可用')
  await expect(card.getByRole('button', { name: '重试' })).toBeVisible()
  // 取不到数据时 KPI 回退占位；重试成功后回升（两步都钉住）。
  await expectKpi(page, '账号总数', '—')
  await expect(diagnosticsTable(page).locator('tbody tr')).toHaveCount(4)

  await card.getByRole('button', { name: '重试' }).click()
  await expect(signTable(page).locator('tbody tr')).toHaveCount(4)
  await expectKpi(page, '账号总数', '4个')
})

test('invariant 6: a failing diagnostics read leaves the sign-status card usable', async ({
  page,
}) => {
  await mockDashboardApi(page, {
    role: 'user',
    diagnosticsError: {
      status: 500,
      code: 'internal_error',
      message: '诊断暂不可用',
      once: true,
    },
  })
  await openDashboard(page)

  const card = page.locator('.card').filter({ hasText: '签到诊断' })
  await expect(card).toContainText('诊断暂不可用')
  await expect(card.getByRole('button', { name: '重试' })).toBeVisible()
  await expect(signTable(page).locator('tbody tr')).toHaveCount(4)
  await expectKpi(page, '账号总数', '4个')

  await card.getByRole('button', { name: '重试' }).click()
  await expect(diagnosticsTable(page).locator('tbody tr')).toHaveCount(4)
})

test('invariant 6: a failing capacity read leaves the KPI usable and recovers on retry', async ({
  page,
}) => {
  await mockDashboardApi(page, {
    role: 'admin',
    capacityError: {
      status: 500,
      code: 'internal_error',
      message: '容量暂不可用',
      once: true,
    },
  })
  await openDashboard(page)

  const card = capacityCard(page)
  await expect(card).toContainText('容量暂不可用')
  await expect(card.getByRole('button', { name: '重试' })).toBeVisible()
  await expectKpi(page, '任务总数', '4项')
  await expectKpi(page, '窗口已报', '3项')
  await expectKpi(page, '停用任务', '1项')

  await card.getByRole('button', { name: '重试' }).click()
  await expect(card.locator('.app-metric')).toHaveCount(3)
  await expect
    .poll(() => visibleText(card.locator('.app-metric').filter({ hasText: '容量' })))
    .toBe('容量40')
})

test('invariant 6: a failing window read marks the capacity card and leaves KPI dashes', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, {
    role: 'admin',
    windowsError: { status: 500, code: 'internal_error', message: '窗口暂不可用' },
  })
  await openDashboard(page)

  const card = capacityCard(page)
  await expect(card).toContainText('窗口暂不可用')
  await expect(card.getByRole('button', { name: '重试' })).toBeVisible()
  await expectKpi(page, '任务总数', '—')
  expect(capacityCalls(api), 'no options means no capacity request').toHaveLength(0)
})
