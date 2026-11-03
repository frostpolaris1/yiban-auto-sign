import { test, expect, type Page } from '@playwright/test'
import { visibleText } from './support/visible-text'
import type { CapacityReport } from '../src/lib/api/schedule'
import type { SignStatsDay } from '../src/lib/api/stats'
import {
  DASHBOARD_DAY,
  DEFAULT_PREF_STATS,
  DEFAULT_SIGN_STATS,
  type DashboardApiCall,
  accountSummaryCalls,
  capacityCalls,
  diagnosticsCalls,
  lastCapacityCall,
  mockDashboardApi,
  prefStatsCalls,
  signStatsCalls,
  signStatusCalls,
  windowsCalls,
} from './support/fake-dashboard-api'

/**
 * 看板的九条不变式。
 *
 * 1. 角色取数隔离：user 零窗口与容量请求；master/admin 零签到状态与诊断请求。
 * 2. KPI 由条目派生，四项计数与列表行数一致；空账号全零。
 * 3. 轮询：30 秒后自动再取一次；页面隐藏时不轮询。
 * 4. 过期标记：stale=true 的行带「数据已过期」，全 false 时一处也没有。
 * 5. 容量体检：默认选第一可用任务并即发请求；切换选择器触发新请求；
 *    shortfall>0 用 danger 徽章；缺口建议按瓶颈给配置动作（通道给数字与复制，
 *    出口只给配置名），无缺口不渲染建议块。
 * 6. 空态与错误态：无账号两卡空态；空窗口说「今日无可用窗口」；
 *    单卡失败只挂该卡，另一卡照常；窗口计数以注脚形式留在容量卡。
 * 7. 展示模式零请求：`e2e/showcase.spec.ts`。
 * 8. 聚合图：master/admin 三张图与两个聚合请求；user 零图零请求；
 *    空数据说原因不画轴；单图失败只挂本图并单独重试；自选图跟随任务选择。
 * 9. 今日终态 KPI：master/admin 四卡取报告最后两天；成功率＝成功÷（成功+失败），
 *    跳过不计入；分母 0 显「—」；环比 pill 分「有对比/无对比」两态；
 *    KPI 与图表共用同一次 sign-stats 读取，不新增请求；
 *    业务日跨日换键恰好重取一次，同一业务日内不轮询。
 *
 * 断言用户可见结果与请求参数，不钉组件内部结构。
 */

function kpiCard(page: Page, label: string) {
  // 精确匹配标签：`今日成功` 是 `今日成功率` 的子串，按整卡文本过滤会双命中。
  return page.locator('.kpi-card').filter({ has: page.getByText(label, { exact: true }) })
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

/** 报告末两天换成给定桶值（缺省 0）：KPI 只读最后两项，前面照抄缺省夹具。 */
function statsWithTail(
  yesterday: Partial<SignStatsDay>,
  today: Partial<SignStatsDay>,
): typeof DEFAULT_SIGN_STATS {
  const day = (over: Partial<SignStatsDay>): SignStatsDay => ({
    day: '2026-10-01',
    pending: 0,
    running: 0,
    success: 0,
    failed: 0,
    skipped: 0,
    ...over,
  })
  return {
    ...DEFAULT_SIGN_STATS,
    days: [
      ...DEFAULT_SIGN_STATS.days.slice(0, 28),
      { ...day(yesterday), day: '2026-10-27' },
      { ...day(today), day: '2026-10-28' },
    ],
  }
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

test('invariant 5: a channel-bound shortfall suggests a worker count and copies it', async ({
  page,
}) => {
  // 剪贴板用确定性替身：只断言页面确实把配置行交给 writeText。
  await page.addInitScript(() => {
    const copied: string[] = []
    Object.defineProperty(window, '__copiedConfig', { value: copied, configurable: true })
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          copied.push(text)
        },
      },
    })
  })
  await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  const card = capacityCard(page)
  // 缺省任务无缺口：即使响应带建议值也不渲染建议块。
  await expect(card.locator('.app-capacity-hint')).toHaveCount(0)
  await expect(card.getByText(/YIBAN_WORKERS/)).toHaveCount(0)

  await page.getByLabel('任务').selectOption('school-beta:task-night')
  const hint = card.locator('.app-capacity-hint')
  await expect(hint).toContainText('通道是瓶颈：把 YIBAN_WORKERS 提到 4。')
  await expect(hint.locator('code')).toHaveText('YIBAN_WORKERS=4')

  await hint.getByRole('button', { name: '复制' }).click()
  await expect(page.getByText('已复制配置行')).toBeVisible()
  const copied = await page.evaluate(
    () => (window as unknown as { __copiedConfig: string[] }).__copiedConfig,
  )
  expect(copied).toEqual(['YIBAN_WORKERS=4'])
})

test('invariant 5: a failed copy leaves a visible hint instead of a silent no-op', async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async () => {
          throw new Error('clipboard denied')
        },
      },
    })
  })
  await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  await page.getByLabel('任务').selectOption('school-beta:task-night')
  const hint = capacityCard(page).locator('.app-capacity-hint')
  await hint.getByRole('button', { name: '复制' }).click()
  await expect(hint.getByRole('alert')).toHaveText('复制失败，请手动选择文本')
  await expect(page.getByText('已复制配置行')).toHaveCount(0)
})

test('invariant 5: an egress-bound shortfall points at the rate, never a number', async ({
  page,
}) => {
  const egress: CapacityReport = {
    school_id: 'school-beta',
    task_id: 'task-night',
    day: DASHBOARD_DAY,
    source: 'override',
    window_start: `${DASHBOARD_DAY}T20:00:00+08:00`,
    window_end: `${DASHBOARD_DAY}T21:00:00+08:00`,
    capacity: 20,
    demand: 26,
    shortfall: 6,
    binding: 'egress_rate',
    // 出口瓶颈即使带了数字也不展示：加并发解决不了。
    suggested_workers: 7,
  }
  await mockDashboardApi(page, { role: 'admin', capacity: egress })
  await openDashboard(page)

  const card = capacityCard(page)
  const hint = card.locator('.app-capacity-hint')
  await expect(hint).toContainText('出口速率是瓶颈：提高 YIBAN_EGRESS_RATE 或增加出口标识。')
  await expect(hint.locator('code')).toHaveText('YIBAN_EGRESS_RATE')
  await expect(hint).not.toContainText('YIBAN_WORKERS')
  await expect(hint).not.toContainText('7')
  await expect(hint.getByRole('button', { name: '复制' })).toHaveCount(0)
})

test('invariant 6: an empty window list says so and sends no capacity request', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'admin', windows: [] })
  await openDashboard(page)

  await expect(page.getByText('今日无可用窗口')).toBeVisible()
  await expect(capacityCard(page)).toContainText('今日窗口 0 项 · 已报 0 · 停用 0 · 可用 0')
  expect(capacityCalls(api), 'no task means no capacity request').toHaveLength(0)
  expect(prefStatsCalls(api), 'no task means no crowding request').toHaveLength(0)
  await expect(chartCard(page, '签到时段拥挤度')).toContainText(
    '暂无可用任务，暂不统计自选人数。',
  )
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
  // 窗口计数已并入注脚：容量失败不连坐，注脚照常可读。
  await expect(card).toContainText('今日窗口 4 项 · 已报 3 · 停用 1 · 可用 2')
  await expectKpi(page, '今日成功', '3个')

  await card.getByRole('button', { name: '重试' }).click()
  await expect(card.locator('.app-metric')).toHaveCount(3)
  await expect
    .poll(() => visibleText(card.locator('.app-metric').filter({ hasText: '容量' })))
    .toBe('容量40')
})

test('invariant 6: a failing window read marks the capacity card and leaves the KPI unaffected', async ({
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
  // 窗口读不到就没有注脚；KPI 走统计报告，不随窗口失败变虚线。
  await expect(card).not.toContainText('今日窗口')
  await expectKpi(page, '今日成功', '3个')
  expect(capacityCalls(api), 'no options means no capacity request').toHaveLength(0)
})

function chartCard(page: Page, title: string) {
  return page.locator('.card').filter({ hasText: title })
}

test('invariant 8: a manager gets three aggregate charts and both stats reads', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  await expect(page.getByText('30 天签到趋势')).toBeVisible()
  await expect(page.getByText('状态分布')).toBeVisible()
  await expect(page.getByText('签到时段拥挤度')).toBeVisible()
  await expect(page.locator('.app-chart-canvas')).toHaveCount(3)
  expect(signStatsCalls(api), 'one read for trend and distribution').toHaveLength(1)
  expect(prefStatsCalls(api), 'one read for the selected task').toHaveLength(1)
  expect(prefStatsCalls(api)[0].params['school_id']).toBe('school-alpha')
  expect(prefStatsCalls(api)[0].params['task_id']).toBe('task-daily')
})

test('invariant 8: a user dashboard renders no aggregate charts', async ({ page }) => {
  const api = await mockDashboardApi(page, { role: 'user' })
  await openDashboard(page)
  await expect(signTable(page)).toBeVisible()

  expect(signStatsCalls(api), 'a user never reads sign statistics').toHaveLength(0)
  expect(prefStatsCalls(api), 'a user never reads crowding').toHaveLength(0)
  await expect(page.getByText('30 天签到趋势')).toHaveCount(0)
  await expect(page.locator('.app-chart-canvas')).toHaveCount(0)
})

test('invariant 8: empty aggregates name the reason instead of drawing axes', async ({
  page,
}) => {
  const emptyStats = {
    ...DEFAULT_SIGN_STATS,
    days: DEFAULT_SIGN_STATS.days.map((day) => ({
      ...day,
      pending: 0,
      running: 0,
      success: 0,
      failed: 0,
      skipped: 0,
    })),
    by_status: { pending: 0, running: 0, success: 0, failed: 0, skipped: 0 },
    total: 0,
    days_missing: [],
  }
  await mockDashboardApi(page, {
    role: 'admin',
    signStats: emptyStats,
    prefStats: { ...DEFAULT_PREF_STATS, slots: [] },
  })
  await openDashboard(page)

  await expect(chartCard(page, '30 天签到趋势')).toContainText('近 30 天暂无签到记录。')
  await expect(chartCard(page, '状态分布')).toContainText('窗口内暂无账号终态。')
  await expect(chartCard(page, '签到时段拥挤度')).toContainText('当前窗口没有可展示的时间片。')
})

test('invariant 8: a stats failure stays on its own cards and retries alone', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, {
    role: 'admin',
    signStatsError: { status: 500, code: 'internal_error', message: '统计暂不可用', once: true },
  })
  await openDashboard(page)

  const trend = chartCard(page, '30 天签到趋势')
  await expect(trend).toContainText('统计暂不可用')
  await expect(capacityCard(page)).toBeVisible()
  // KPI 与图表共用同一份报告：读不到就一起变虚线，不编数字。
  await expectKpi(page, '今日成功', '—')
  await expectKpi(page, '今日成功率', '—')
  const before = signStatsCalls(api).length

  await trend.getByRole('button', { name: '重试' }).click()
  await expect.poll(() => signStatsCalls(api).length).toBe(before + 1)
  await expect(page.locator('.app-chart-canvas')).toHaveCount(3)
  // 重试成功后 KPI 与图表一起回升。
  await expectKpi(page, '今日成功', '3个')
})

test('invariant 8: the crowding chart follows the selected task', async ({ page }) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)
  await expect.poll(() => prefStatsCalls(api).length).toBe(1)

  await page.getByLabel('任务').selectOption('school-beta:task-night')
  await expect
    .poll(() => prefStatsCalls(api).at(-1)?.params['task_id'])
    .toBe('task-night')
  // 假 API 按任务给不同人数：摘要随数据变，而不是只换请求参数。
  await expect(chartCard(page, '签到时段拥挤度')).toContainText('最热 20:10（3 人）')
})

test('invariant 8: switching the theme rebuilds the charts', async ({ page }) => {
  await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)
  const canvas = page.locator('.app-chart-canvas').first()
  await expect(canvas).toBeVisible()

  const before = await canvas.screenshot()
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  await expect
    .poll(async () => (await canvas.screenshot()).equals(before))
    .toBe(false)
})

test('invariant 9: the manager KPI reads today from the same stats report', async ({ page }) => {
  const api = await mockDashboardApi(page, {
    role: 'admin',
    // 昨日 4/4（成功率 50）；今日 5/2/待3/跑1/跳9（成功率 5÷7≈71.4）。
    signStats: statsWithTail(
      { success: 4, failed: 4, skipped: 2 },
      { pending: 3, running: 1, success: 5, failed: 2, skipped: 9 },
    ),
  })
  await openDashboard(page)

  await expectKpi(page, '今日成功', '5个')
  await expectKpi(page, '今日失败', '2个')
  await expectKpi(page, '今日成功率', '71.4%')
  await expectKpi(page, '进行中/待签', '4个')

  // 昨日 50 → 今日 71.4：差 21.4 个百分点，向上 pill。
  const pill = kpiCard(page, '今日成功率').locator('.kpi-pill')
  await expect(pill).toContainText('较昨日 +21.4pp')
  await expect(pill).toHaveClass(/up/)

  // KPI 复用图表那份报告：一次读取，不新增请求。
  expect(signStatsCalls(api), 'KPI and charts share one read').toHaveLength(1)
})

test('invariant 9: skipped states never enter the rate denominator', async ({ page }) => {
  await mockDashboardApi(page, {
    role: 'admin',
    // 今日 3/3/跳9：跳过不计入 → 3÷6=50%；昨日只有跳过 → 无终态，无对比。
    signStats: statsWithTail(
      { skipped: 5 },
      { success: 3, failed: 3, skipped: 9 },
    ),
  })
  await openDashboard(page)

  await expectKpi(page, '今日成功率', '50%')
  const pill = kpiCard(page, '今日成功率').locator('.kpi-pill')
  await expect(pill).toContainText('无昨日对比')
  await expect(pill).toHaveClass(/flat/)
})

test('invariant 9: a zero denominator shows an em dash without a pill', async ({ page }) => {
  await mockDashboardApi(page, {
    role: 'admin',
    // 今日只有待签：成功+失败为 0 → 「—」；昨日有终态也不出对比。
    signStats: statsWithTail({ success: 1 }, { pending: 2 }),
  })
  await openDashboard(page)

  await expectKpi(page, '今日成功', '0个')
  await expectKpi(page, '今日失败', '0个')
  await expectKpi(page, '今日成功率', '—')
  await expect(kpiCard(page, '今日成功率').locator('.kpi-pill')).toHaveCount(0)
})

test('invariant 9: the rate pill turns down when today trails yesterday', async ({ page }) => {
  await mockDashboardApi(page, {
    role: 'admin',
    // 昨日 4/1（80%）；今日 2/3（40%）：差 -40 个百分点，向下 pill。
    signStats: statsWithTail(
      { success: 4, failed: 1 },
      { success: 2, failed: 3 },
    ),
  })
  await openDashboard(page)

  await expectKpi(page, '今日成功率', '40%')
  const pill = kpiCard(page, '今日成功率').locator('.kpi-pill')
  await expect(pill).toContainText('较昨日 -40pp')
  await expect(pill).toHaveClass(/down/)
})

test('invariant 9: the header creates an account and exports nothing', async ({ page }) => {
  await mockDashboardApi(page, { role: 'master' })
  await openDashboard(page)

  // master 与 admin 同口径：KPI 来自缺省报告的末两天。
  await expectKpi(page, '今日成功', '3个')
  await expectKpi(page, '今日成功率', '100%')
  await expect(page.getByRole('button', { name: '导出' })).toHaveCount(0)

  await page.getByRole('button', { name: '新建账号' }).click()
  await expect(page).toHaveURL(/\/accounts$/)
})

test('invariant 9: the pending KPI reads the summary and links to accounts', async ({ page }) => {
  const api = await mockDashboardApi(page, {
    role: 'admin',
    accountSummary: { pending: 2, rejected: 1 },
  })
  await openDashboard(page)

  await expectKpi(page, '待处理账号', '3个')
  const card = kpiCard(page, '待处理账号')
  await expect(card).toContainText('待审核 2 · 已拒绝 1')
  expect(accountSummaryCalls(api), 'one summary read').toHaveLength(1)

  await card.click()
  await expect(page).toHaveURL(/\/accounts$/)
})

test('invariant 9: a user never reads the account summary', async ({ page }) => {
  const api = await mockDashboardApi(page, { role: 'user' })
  await openDashboard(page)

  expect(accountSummaryCalls(api), 'a user never reads the summary').toHaveLength(0)
  await expect(page.locator('.kpi-card').filter({ hasText: '待处理账号' })).toHaveCount(0)
})

test('invariant 9: a failed summary keeps the pending card at an em dash', async ({ page }) => {
  await mockDashboardApi(page, {
    role: 'admin',
    accountSummaryError: { status: 500, code: 'internal_error', message: '汇总不可用' },
  })
  await openDashboard(page)

  await expectKpi(page, '待处理账号', '—')
  await expect(kpiCard(page, '待处理账号')).toContainText('等待读取')
})

test('invariant 9: the stats read refetches once when the business day rolls over', async ({
  page,
}) => {
  // +08:00 的 23:59:30 进入看板：60 秒后跨过零点，业务日换键。
  await page.clock.install({ time: new Date('2026-11-03T15:59:30Z') })
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  // 首屏一次读取：KPI 与图表共用。
  await expect.poll(() => signStatsCalls(api).length).toBe(1)

  // 跨到 +08:00 00:00:30：业务日换了缓存键，恰好再取一次。
  await page.clock.runFor(60_000)
  await expect.poll(() => signStatsCalls(api).length, 'the rollover refetches once').toBe(2)

  // 仍在同一业务日：60 秒定时器照跑，但不再取数。
  await page.clock.runFor(60_000)
  await expect(signStatsCalls(api).length, 'the same business day does not refetch').toBe(2)
})
