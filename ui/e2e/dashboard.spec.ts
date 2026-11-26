import { test, expect, type Page } from '@playwright/test'
import { visibleText } from './support/visible-text'
import type { CapacityReport } from '../src/lib/api/schedule'
import type { SignStatsDay } from '../src/lib/api/stats'
import {
  DASHBOARD_DAY,
  DEFAULT_AUDIT_STATUS,
  DEFAULT_ENGINE_SNAPSHOT,
  DEFAULT_PREF_STATS,
  DEFAULT_SIGN_STATS,
  type DashboardApiCall,
  accountSummaryCalls,
  auditStatusCalls,
  capacityCalls,
  diagnosticsCalls,
  engineSnapshotCalls,
  lastCapacityCall,
  mockDashboardApi,
  pauseCalls,
  prefStatsCalls,
  signStatsCalls,
  signStatusCalls,
  windowsCalls,
} from './support/fake-dashboard-api'

/**
 * 看板的十条不变式。
 *
 * 1. 角色取数隔离：user 一律 302 到 /app，零窗口、容量、审计巡检与引擎快照请求；
 *    master/admin 零签到状态与诊断请求。
 * 2. （已退役）签到状态表随 SignStatusCard 删除；今日 KPI 改由 30 天报告派生，见第 9 条。
 * 3. 轮询：拥挤度 15 秒后自动再取一次；页面隐藏时不轮询。
 * 4. （已退役）过期标记随签到状态表一并删除。
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
 * 10. 审计链巡检仅 master：读一次 audit-status 并显示三态、行数与时刻；
 *    admin 不请求、不渲染巡检条（台账仍可读）；断链给定位行且不弹窗；
 *    空链说「暂无审计行」，不算通过。
 *    桌面快捷条与侧栏 3/3 重复，已隐藏（B5）；移动保留且健康条折叠为摘要行。
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

function auditCard(page: Page) {
  return page.locator('.card').filter({ hasText: '审计链巡检' })
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

test('invariant 1: a user is sent to /app and reads no console dashboard data', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'user' })
  await page.goto('/')
  await expect(page, 'user never lands the console dashboard').toHaveURL(/\/app$/)
  await expect(page.locator('.d-sidebar')).toHaveCount(0)
  await expect(page.getByText('容量体检')).toHaveCount(0)
  await expect(page.getByText('审计链巡检')).toHaveCount(0)

  expect(windowsCalls(api), 'a redirected user reads no window list').toHaveLength(0)
  expect(capacityCalls(api), 'a redirected user reads no capacity').toHaveLength(0)
  expect(auditStatusCalls(api), 'a redirected user inspects no audit chain').toHaveLength(0)
  expect(engineSnapshotCalls(api), 'a redirected user reads no engine snapshot').toHaveLength(0)
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

test('invariant 10: the audit card shows the verdict, rows and inspection time', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'master' })
  await openDashboard(page)

  const card = auditCard(page)
  await expect(card).toContainText('链完好')
  await expect(card).toContainText('巡检 128 行')
  await expect(card).toContainText('2026/10/28')
  // 按需巡检一次；链校验开销随行数增长，不进轮询。
  expect(auditStatusCalls(api), 'one inspection when the card mounts').toHaveLength(1)
})

test('invariant 10: a broken chain names the row without a popup', async ({ page }) => {
  await mockDashboardApi(page, {
    role: 'master',
    auditStatus: {
      ...DEFAULT_AUDIT_STATUS,
      ok: false,
      checked_rows: 42,
      last_hash: '',
      broken_at: 42,
    },
  })
  await openDashboard(page)

  const card = auditCard(page)
  await expect(card).toContainText('链异常')
  await expect(card).toContainText('第 42 行')
  await expect(card).toContainText('本页不展示审计正文')
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('invariant 10: an empty chain is neutral, not an alarm', async ({ page }) => {
  await mockDashboardApi(page, {
    role: 'master',
    auditStatus: {
      ...DEFAULT_AUDIT_STATUS,
      ok: false,
      empty: true,
      checked_rows: 0,
      last_hash: '',
    },
  })
  await openDashboard(page)

  const card = auditCard(page)
  await expect(card).toContainText('暂无审计行')
  await expect(card).toContainText('空链不算通过')
  await expect(card.getByText('链异常')).toHaveCount(0)
})

test('invariant 10: an admin neither requests nor renders the audit inspection', async ({ page }) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
  await expect(strip).toContainText('执行引擎')
  await expect(strip.getByText('审计', { exact: true })).toHaveCount(0)
  await expect(strip.getByText('审计链巡检')).toHaveCount(0)
  expect(auditStatusCalls(api), 'an admin never inspects the audit chain').toHaveLength(0)
})

test('invariant 10: the health pulse bar shows engine status, live counters and recovers from failure alone', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, {
    role: 'master',
    engineSnapshot: {
      ...DEFAULT_ENGINE_SNAPSHOT,
      paused: true,
      due_now: 4,
      retrying_truncated: true,
    },
    engineError: {
      status: 500,
      code: 'internal_error',
      message: '引擎快照暂不可用',
      once: true,
    },
  })
  await openDashboard(page)

  const strip = auditCard(page)
  await expect(strip).toContainText('引擎快照暂不可用')
  await expect(strip).toContainText('链完好')
  await expectKpi(page, '今日成功', '3个')

  await strip.getByRole('button', { name: '重试' }).click()
  await expect(strip).toContainText('引擎已暂停')
  await expect(strip).toContainText('在飞 1')
  await expect(strip).toContainText('待执行 4')
  await expect(strip).toContainText('待重试 1+')
  expect(engineSnapshotCalls(api).length).toBe(2)
})

test('invariant 3: the crowding card polls again after fifteen seconds', async ({ page }) => {
  await page.clock.install()
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  await expect.poll(() => prefStatsCalls(api).length, 'one read for the selected task').toBe(1)

  await page.clock.runFor(12_000)
  expect(prefStatsCalls(api).length, 'no poll before the interval elapses').toBe(1)

  await page.clock.runFor(4_000)
  await expect.poll(() => prefStatsCalls(api).length).toBe(2)
})

test('invariant 3: a hidden page does not poll', async ({ page }) => {
  await page.clock.install()
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)
  await expect.poll(() => prefStatsCalls(api).length).toBe(1)

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    window.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(40_000)
  expect(prefStatsCalls(api).length, 'a hidden page sends no poll').toBe(1)

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    window.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(16_000)
  await expect.poll(() => prefStatsCalls(api).length).toBe(2)
})

test('invariant 5: capacity defaults to the first usable task and follows the picker', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  const taskPicker = page.getByLabel('任务')
  await expect(taskPicker).toContainText('合成学校甲 · 每日签到')
  await expect.poll(() => capacityParams(lastCapacityCall(api), 'school_id')).toBe('school-alpha')
  expect(capacityParams(lastCapacityCall(api), 'task_id')).toBe('task-daily')
  // 请求日按渲染时计算；只钉形状，避免跨 00:00 的假红。
  expect(capacityParams(lastCapacityCall(api), 'day'), 'the day rides the request').toMatch(
    /^\d{4}-\d{2}-\d{2}$/,
  )

  // 选项清单只在打开时可见：断言后先关菜单，选乙放到下方步骤。
  await taskPicker.click()
  const options = page.getByRole('option')
  await expect(options).toHaveCount(2)
  await expect(options.nth(0)).toHaveText('合成学校甲 · 每日签到')
  await expect(options.nth(1)).toHaveText('合成学校乙 · 夜间签到')
  await page.keyboard.press('Escape')

  const card = capacityCard(page)
  const gap = card.locator('.app-metric').filter({ hasText: '缺口' })
  await expect(gap.locator('.app-metric-value')).toHaveClass(/is-success/)
  await expect.poll(() => visibleText(gap.locator('.app-metric-value'))).toBe('0')
  await expect(card).toContainText('08:30–09:00')
  await expect(card.locator('.badge').filter({ hasText: '平台' })).toBeVisible()

  await taskPicker.click()
  await page.getByRole('option', { name: '合成学校乙 · 夜间签到' }).click()
  await expect(taskPicker).toContainText('合成学校乙 · 夜间签到')
  await expect.poll(() => capacityParams(lastCapacityCall(api), 'school_id')).toBe('school-beta')
  expect(capacityParams(lastCapacityCall(api), 'task_id')).toBe('task-night')
  expect(capacityParams(lastCapacityCall(api), 'day')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  await expect.poll(() => visibleText(gap.locator('.app-metric-value'))).toBe('6')
  await expect(gap.locator('.app-metric-value')).toHaveClass(/is-danger/)
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

  await page.getByLabel('任务').click()
  await page.getByRole('option', { name: '合成学校乙 · 夜间签到' }).click()
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

  await page.getByLabel('任务').click()
  await page.getByRole('option', { name: '合成学校乙 · 夜间签到' }).click()
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

test('invariant 8: a manager gets two aggregate charts and both stats reads', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  await expect(page.getByText('30 天签到趋势')).toBeVisible()
  await expect(page.getByText('签到时段拥挤度')).toBeVisible()
  await expect(page.getByText('状态分布')).toHaveCount(0)
  await expect(page.locator('.app-chart-canvas')).toHaveCount(2)
  expect(signStatsCalls(api), 'one read for trend and KPI').toHaveLength(1)
  expect(prefStatsCalls(api), 'one read for the selected task').toHaveLength(1)
  expect(prefStatsCalls(api)[0].params['school_id']).toBe('school-alpha')
  expect(prefStatsCalls(api)[0].params['task_id']).toBe('task-daily')
})

test('invariant 8: a user is sent to /app and reads no aggregate charts', async ({ page }) => {
  const api = await mockDashboardApi(page, { role: 'user' })
  await page.goto('/')
  await expect(page, 'user never lands the console dashboard').toHaveURL(/\/app$/)

  expect(signStatsCalls(api), 'a redirected user reads no sign statistics').toHaveLength(0)
  expect(prefStatsCalls(api), 'a redirected user reads no crowding').toHaveLength(0)
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
  await expectKpi(page, '今日失败', '—')
  const before = signStatsCalls(api).length

  await trend.getByRole('button', { name: '重试' }).click()
  await expect.poll(() => signStatsCalls(api).length).toBe(before + 1)
  await expect(page.locator('.app-chart-canvas')).toHaveCount(2)
  // 重试成功后 KPI 与图表一起回升。
  await expectKpi(page, '今日成功', '3个')
})

test('invariant 8: the crowding chart follows the selected task', async ({ page }) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)
  await expect.poll(() => prefStatsCalls(api).length).toBe(1)

  await page.getByLabel('任务').click()
  await page.getByRole('option', { name: '合成学校乙 · 夜间签到' }).click()
  await expect
    .poll(() => prefStatsCalls(api).at(-1)?.params['task_id'])
    .toBe('task-night')
  // 假 API 按任务给不同人数：摘要随数据变，而不是只换请求参数。
  await expect(chartCard(page, '签到时段拥挤度')).toContainText('高峰 20:10（3 人）')
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
  await expectKpi(page, '进行中/待签', '4个')
  await expectKpi(page, '待处理账号', '3个')

  // 口径裁决：总览不再展示成功率卡；待处理计数来自 summary。
  await expect(page.locator('.kpi-card').filter({ hasText: '今日成功率' })).toHaveCount(0)
  await expect(kpiCard(page, '待处理账号')).toContainText('待审核 2 · 已拒绝 1 →')

  // KPI 复用图表那份报告：一次读取，不新增请求。
  expect(signStatsCalls(api), 'KPI and charts share one read').toHaveLength(1)
})

test('invariant 9: the dashboard header carries no create entry (old-line habit)', async ({
  page,
}) => {
  await mockDashboardApi(page, { role: 'master' })
  await openDashboard(page)

  // master 与 admin 同口径：KPI 来自缺省报告的末两天 + 待处理汇总。
  await expectKpi(page, '今日成功', '3个')
  await expectKpi(page, '待处理账号', '3个')
  await expect(page.getByRole('button', { name: '导出' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '新建账号' })).toHaveCount(0)
})

test('invariant 9: the pending KPI sits in slot 4 and links straight to accounts', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, {
    role: 'admin',
    accountSummary: { pending: 2, rejected: 1 },
  })
  await openDashboard(page)

  const labels = await page.locator('.kpi-card .kpi-label').allTextContents()
  expect(labels).toEqual(['今日成功', '今日失败', '进行中/待签', '待处理账号'])

  const card = kpiCard(page, '待处理账号')
  await expectKpi(page, '待处理账号', '3个')
  await expect(card).toContainText('待审核 2 · 已拒绝 1 →')
  expect(accountSummaryCalls(api), 'one summary read').toHaveLength(1)

  await card.click()
  await expect(page).toHaveURL(/\/accounts$/)
})

test('invariant 9: the trend card switches between trend and distribution tabs', async ({
  page,
}) => {
  await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  const trendTab = page.getByRole('tab', { name: '趋势' })
  const mixTab = page.getByRole('tab', { name: '分布' })
  await expect(trendTab).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByLabel('近 30 天每日签到终态堆叠柱状图')).toBeVisible()

  await mixTab.click()
  await expect(mixTab).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByLabel('窗口内账号终态分布环形图')).toBeVisible()
  await expect(page.locator('.card').filter({ hasText: '窗口内状态分布' })).toContainText(
    '按状态去重',
  )

  await trendTab.click()
  await expect(page.getByLabel('近 30 天每日签到终态堆叠柱状图')).toBeVisible()
})

test('invariant 9: the health strip links engine chips and the run ledger', async ({ page }) => {
  await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  const strip = page.locator('.app-health-strip')
  await strip.getByRole('link', { name: /在飞/ }).click()
  await expect(page).toHaveURL(/\/logs\?tab=patrol$/)
  await page.goBack()
  await page.locator('.app-health-strip').getByRole('link', { name: '运行台账 →' }).click()
  await expect(page).toHaveURL(/\/logs\?tab=events$/)
})

test('invariant 9: a user is sent to /app and never reads the account summary', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'user' })
  await page.goto('/')
  await expect(page, 'user never lands the console dashboard').toHaveURL(/\/app$/)

  expect(accountSummaryCalls(api), 'a redirected user reads no summary').toHaveLength(0)
  await expect(page.locator('.kpi-card').filter({ hasText: '待处理账号' })).toHaveCount(0)
})

test('invariant 9: a failed summary keeps the pending KPI at an em dash', async ({ page }) => {
  await mockDashboardApi(page, {
    role: 'admin',
    accountSummaryError: { status: 500, code: 'internal_error', message: '汇总不可用' },
  })
  await openDashboard(page)

  // 汇总未知：待处理卡显「—」，不编 0；其余 KPI 照常。
  await expectKpi(page, '待处理账号', '—')
  await expect(kpiCard(page, '待处理账号')).toContainText('等待读取')
  await expectKpi(page, '今日成功', '3个')
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

test('invariant 10: failed KPI links to failed events and the desktop yields quick links to the sidebar', async ({
  page,
}) => {
  await mockDashboardApi(page, {
    role: 'admin',
    signStats: {
      days: [
        { day: '2026-10-27', pending: 0, running: 0, success: 4, failed: 0, skipped: 0 },
        { day: '2026-10-28', pending: 1, running: 0, success: 3, failed: 2, skipped: 0 },
      ],
      total: 6,
      by_status: { pending: 1, running: 0, success: 3, failed: 2, skipped: 0 },
      days_missing: [],
    },
  })
  await openDashboard(page)

  // B5（S-10）：桌面快捷条与侧栏 3/3 重复，已隐藏；入口由侧栏承担。
  // 移动端保留快捷条的 44px 命中由上方 B5 用例断言。
  const quickLinks = page.locator('nav[aria-label="管理常用直达"]')
  await expect(quickLinks).toHaveCount(1)
  await expect(quickLinks).toBeHidden()
  const sidebar = page.getByRole('complementary', { name: '主导航' })
  await expect(sidebar.getByRole('link', { name: '待办', exact: true })).toHaveAttribute('href', '/queue')
  await expect(sidebar.getByRole('link', { name: '审计日志', exact: true })).toHaveAttribute('href', '/audit')
  await expect(sidebar.getByRole('link', { name: '系统设置', exact: true })).toHaveAttribute('href', '/system')

  await kpiCard(page, '今日失败').click()
  await expect(page).toHaveURL(/\/logs\?.*tab=events.*state=failed|\/logs\?.*state=failed.*tab=events/)
})


test('invariant 11: an admin sees pause disabled on an unpaused engine and sends nothing', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'admin' })
  await openDashboard(page)

  const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
  const pause = strip.getByRole('button', { name: '暂停引擎' })
  await expect(pause).toBeDisabled()
  await expect(pause).toHaveAttribute('title', '仅主管理员可操作引擎开关')
  await pause.click({ force: true })
  expect(pauseCalls(api), 'a disabled pause must not reach the server').toHaveLength(0)
})

test('invariant 11: a master pauses the engine through reauth and the banner offers resume', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, { role: 'master' })
  await openDashboard(page)

  const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
  await strip.getByRole('button', { name: '暂停引擎' }).click()
  const dialog = page.getByRole('dialog', { name: '暂停执行引擎' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('在飞 1 条（本管辖范围）')
  await expect(dialog).toContainText('待执行 0 条（本管辖范围）')
  await expect(dialog).toContainText('暂停与恢复均仅主管理员')
  await dialog.getByRole('textbox', { name: '你的登录口令' }).fill('p@ssword')
  await dialog.getByRole('button', { name: '暂停引擎' }).click()

  await expect(dialog).toBeHidden()
  const pauses = pauseCalls(api)
  expect(pauses, 'one full-replacement PUT per confirm').toHaveLength(1)
  expect(pauses[0].body).toEqual({ paused: true, current_password: 'p@ssword' })
  await expect(strip).toContainText('执行引擎已暂停')
  await expect(strip).toContainText('本管辖范围：在飞 1 条任务继续完成；待执行 0 条暂停领取')
  await expect(strip).toContainText('引擎已暂停')
  await expect(strip.getByRole('button', { name: '恢复引擎' })).toBeEnabled()
})

test('invariant 11: an admin sees the resume button disabled with the master-only reason and sends nothing', async ({
  page,
}) => {
  const api = await mockDashboardApi(page, {
    role: 'admin',
    engineSnapshot: { ...DEFAULT_ENGINE_SNAPSHOT, paused: true },
  })
  await openDashboard(page)

  const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
  await expect(strip).toContainText('执行引擎已暂停')
  const resume = strip.getByRole('button', { name: '恢复引擎' })
  await expect(resume).toBeDisabled()
  await expect(resume).toHaveAttribute('title', '仅主管理员可操作引擎开关')
  await resume.click({ force: true })
  expect(pauseCalls(api), 'a disabled resume must not reach the server').toHaveLength(0)
})


test('invariant 12: the site announcement rides the dashboard top and dismisses in session', async ({
  page,
}) => {
  await mockDashboardApi(page, {
    role: 'admin',
    announcementText: '今晚 22:00 起例行维护，签到计划照常执行。',
  })
  await openDashboard(page)

  const banner = page
    .locator('.app-announcement-banner')
    .getByText('今晚 22:00 起例行维护，签到计划照常执行。')
  await expect(banner).toBeVisible()
  await page
    .locator('.app-announcement-banner')
    .getByRole('button', { name: '关闭' })
    .click()
  await expect(page.locator('.app-announcement-banner')).toHaveCount(0)

  // 本会话内跨路由保留：点击侧栏去台账再点回总览（客户端路由），横幅不得重现。
  const sidebar = page.locator('aside[aria-label="主导航"]')
  await sidebar.getByRole('link', { name: '签到台账' }).click()
  await expect(page.getByRole('heading', { name: /签到\s*台账/ })).toBeVisible()
  await sidebar.getByRole('link', { name: '总览' }).click()
  await expect(page.getByRole('heading', { name: /签到\s*运行/ })).toBeVisible()
  await expect(page.locator('.app-announcement-banner')).toHaveCount(0)

  // 刷新回初值：整页重载后横幅重新出现（本会话只在客户端路由内记住关闭）。
  await page.reload()
  await expect(
    page.locator('.app-announcement-banner').getByText('今晚 22:00 起例行维护，签到计划照常执行。'),
  ).toBeVisible()
})

test('invariant 12: an empty announcement renders no banner', async ({ page }) => {
  await mockDashboardApi(page, { role: 'admin' })
  const announcementSettled = page.waitForResponse(
    (response) => response.url().endsWith('/api/announcement'),
  )
  await openDashboard(page)
  await announcementSettled
  await expect(page.locator('.app-announcement-banner')).toHaveCount(0)
})


test('invariant 13: the pause button stays disabled until the engine snapshot lands', async ({
  page,
}) => {
  await mockDashboardApi(page, { role: 'master' })
  // 快照请求挂在闸门上：先看清「读取中」的禁用态再放行。固定延时在并发负载下会抢跑。
  let releaseEngine = (): void => {}
  const engineGate = new Promise<void>((resolve) => {
    releaseEngine = resolve
  })
  await page.route('**/api/admin/engine', async (route) => {
    // fallback 交回假 API，不能 continue 绕过夹具。
    await engineGate
    await route.fallback()
  })
  await openDashboard(page)

  const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
  const pause = strip.getByRole('button', { name: '暂停引擎' })
  await expect(pause).toBeDisabled()
  await expect(pause).toHaveAttribute('title', '引擎状态读取中')

  releaseEngine()
  await expect(pause).toBeEnabled({ timeout: 10_000 })
})

test.describe('B5 冗余移除与移动健康条（2026-11-25）', () => {
  test('B5: the desktop drops the duplicated quick links while the sidebar keeps all three entries', async ({
    page,
  }) => {
    const api = await mockDashboardApi(page, { role: 'admin' })
    await openDashboard(page)

    // 快捷条仍在 DOM（移动端复用），桌面由样式隐藏，不占首屏。
    // 先断言存在再断言隐藏：0 匹配时 toBeHidden 也会通过，会变成空绿。
    const quickLinks = page.locator('nav[aria-label="管理常用直达"]')
    await expect(quickLinks).toHaveCount(1)
    await expect(quickLinks).toBeHidden()
    expect(auditStatusCalls(api), 'admin 不请求巡检，隐藏快捷条也不新增请求').toHaveLength(0)

    const sidebar = page.getByRole('complementary', { name: '主导航' })
    const entries = [
      { name: '待办', href: '/queue' },
      { name: '审计日志', href: '/audit' },
      { name: '系统设置', href: '/system' },
    ] as const
    for (const entry of entries) {
      await expect(sidebar.getByRole('link', { name: entry.name, exact: true })).toHaveAttribute(
        'href',
        entry.href,
      )
    }
  })

  test('B5: the mobile keeps the quick links with a 44px hit box', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await mockDashboardApi(page, { role: 'admin' })
    await openDashboard(page)

    const quickNav = page.locator('nav[aria-label="管理常用直达"]')
    await expect(quickNav).toBeVisible()
    const entries = [
      { name: '待办处理', href: '/queue' },
      { name: '审计链巡检', href: '/audit' },
      { name: '系统设置', href: '/system' },
    ] as const
    for (const entry of entries) {
      const link = quickNav.getByRole('link', { name: entry.name })
      await expect(link).toHaveAttribute('href', entry.href)
      // ::after 的 -14px 外扩是触控命中区；上下各在 13px 处做真实 hit test（留 1px 余量），
      // 并直接断言伪元素上下都没被单侧裁掉，避免只留上缘仍能命中 30px 的空绿。
      const probe = await link.evaluate((el) => {
        const rect = el.getBoundingClientRect()
        const pseudo = getComputedStyle(el, '::after')
        const hits = [-13, 13].map((dy) => {
          const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + dy)
          return target !== null && el.contains(target)
        })
        return { hits, top: pseudo.top, bottom: pseudo.bottom }
      })
      expect(probe.top, entry.name + ' ::after 上扩 -14px').toBe('-14px')
      expect(probe.bottom, entry.name + ' ::after 下扩 -14px').toBe('-14px')
      expect(probe.hits, entry.name + ' 命中盒覆盖字形上下 13px').toEqual([true, true])
    }
  })

  test('B5: on mobile the health strip folds to a short summary and expands on demand', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await mockDashboardApi(page, { role: 'admin' })
    await openDashboard(page)

    const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
    await expect(strip).toBeVisible()
    const collapsed = await strip.boundingBox()
    expect(collapsed?.height ?? Number.POSITIVE_INFINITY, '折叠高度 ≤150').toBeLessThanOrEqual(150)

    const toggle = strip.getByRole('button', { name: '详情' })
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(strip.getByRole('link', { name: /在飞/ })).toBeHidden()
    await expect(strip.getByRole('link', { name: '运行台账 →' })).toBeHidden()
    await expect(strip.locator('a:visible'), '折叠态可见链接 0 个').toHaveCount(0)
    await toggle.scrollIntoViewIfNeeded()

    const toggleHit = await toggle.evaluate((el) => {
      const rect = el.getBoundingClientRect()
      const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
      return { hit: target !== null && el.contains(target), height: rect.height }
    })
    expect(toggleHit.height, '详情按钮命中高度 ≥44').toBeGreaterThanOrEqual(44)
    expect(toggleHit.hit).toBe(true)

    await toggle.click()
    const collapse = strip.getByRole('button', { name: '收起' })
    await expect(collapse).toHaveAttribute('aria-expanded', 'true')
    await expect(strip.getByRole('link', { name: /在飞/ })).toBeVisible()
    await expect(strip.getByRole('link', { name: '运行台账 →' })).toBeVisible()
    await expect(strip.locator('a:visible'), '展开态 4 个链接：3 个脉冲 chip + 运行台账').toHaveCount(4)

    await strip.getByRole('link', { name: '运行台账 →' }).click()
    await expect(page).toHaveURL(/\/logs\?tab=events$/)
  })

  test('B5: the mobile dashboard height stays inside the documented budget', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await mockDashboardApi(page, { role: 'admin' })
    await openDashboard(page)

    const docHeight = await page.evaluate(() => document.documentElement.scrollHeight)
    expect(docHeight, '移动整页 ≤2100px').toBeLessThanOrEqual(2100)
  })

  test('B5: the desktop health strip stays unfolded without a detail toggle', async ({ page }) => {
    await mockDashboardApi(page, { role: 'admin' })
    await openDashboard(page)

    const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
    const toggle = strip.locator('.app-health-toggle')
    await expect(toggle).toBeAttached()
    await expect(toggle).toBeHidden()
    await expect(strip.getByRole('link', { name: /在飞/ })).toBeVisible()
    // R5：桌面链接目标数不变（3 个脉冲 chip + 运行台账），不因移动折叠改造而丢失。
    await expect(strip.locator('.app-health-bar a')).toHaveCount(4)
    const box = await strip.boundingBox()
    expect(box?.height ?? 0, '桌面条高 107±8').toBeGreaterThanOrEqual(99)
    expect(box?.height ?? 0).toBeLessThanOrEqual(115)
  })

  test('B5: a failed audit read is visible in the mobile summary, not a mute "waiting"', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await mockDashboardApi(page, {
      role: 'master',
      auditStatusError: { status: 500, code: 'internal_error', message: '巡检不可用' },
    })
    await openDashboard(page)

    const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
    // 限定在摘要行内，且必须只有一处：引擎徽章此时正常，不会误配。
    await expect(strip.locator('.app-health-summary').getByText('读取失败')).toHaveCount(1)
    await expect(strip.locator('.app-health-summary').getByText('读取失败')).toBeVisible()
    await expect(strip.getByRole('button', { name: '收起' })).toHaveAttribute('aria-expanded', 'true')
  })

  test('B5: a broken audit chain force-expands the mobile strip', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await mockDashboardApi(page, {
      role: 'master',
      auditStatus: { ...DEFAULT_AUDIT_STATUS, ok: false, broken_at: 42 },
    })
    await openDashboard(page)

    const strip = page.getByRole('region', { name: '系统健康与审计巡检' })
    const toggle = strip.getByRole('button', { name: '收起' })
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await expect(strip.getByText('审计异常')).toBeVisible()
    await expect(strip.getByRole('link', { name: /在飞/ })).toBeVisible()

    // 强制展开可手动收起，也能再次展开。
    await toggle.click()
    await expect(strip.getByRole('button', { name: '详情' })).toHaveAttribute('aria-expanded', 'false')
    await expect(strip.getByRole('link', { name: /在飞/ })).toBeHidden()
    await strip.getByRole('button', { name: '详情' }).click()
    await expect(strip.getByRole('button', { name: '收起' })).toHaveAttribute('aria-expanded', 'true')

    const ovX = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(ovX, '断链强制展开不产生横向滚动').toBe(0)
  })
})
