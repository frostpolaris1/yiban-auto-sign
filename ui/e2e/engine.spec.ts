import { test, expect, type Page } from '@playwright/test'
import {
  engineCalls,
  mockEngineApi,
  ENGINE_SNAPSHOT,
} from './support/fake-engine-api'
import {
  fakeSourceCount,
  fakeSourceEmit,
  fakeSourceFail,
  fakeSourceOpen,
  installFakeEventSource,
} from './support/fake-run-api'

/**
 * 执行监控页的不变式（aqn 验收）。
 *
 * 1. 角色可见性：master/admin 从侧栏进页；user 无导航项，
 *    直接访问只有无权限卡片，且零快照请求。
 * 2. 四指标：待执行、在飞、今日成功、今日失败同屏可读；
 *    在飞 0 与非 0 两种状态都可读；账号只出掩码。
 * 3. 租约剩余随时间变化（客户端每秒递减）；在飞行含认领时刻与学校。
 * 4. 待重试：已试次数、下次执行时刻、结果词齐全；同账号两条并存
 *    （行身份是任务行）；超过 200 条显式提示截断。
 * 5. 最近事件：SSE 业务事件实时进列表，最新在前；断线出
 *    「已停止实时」横幅并可重连；断线期间 10 秒轮询降级。
 * 6. 错误态与空态语义分离：失败渲染 role=alert 的错误条，
 *    空渲染各自的空态文案。
 * 7. 全页不出现「队列」字样；390 宽无横向溢出。
 *
 * 断言用户可见结果与请求次数，不钉内部调用顺序。
 */

/** 等执行监控页正文就绪。不做导航：URL 由调用方给。 */
async function openEngine(page: Page) {
  await expect(page.getByRole('heading', { name: /执行监控/ }).first()).toBeVisible()
  await expect(page.getByRole('heading', { name: '在飞清单' })).toBeVisible()
}

test('invariant 1: master reaches the monitor from the navigation', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, { role: 'master' })
  await page.goto('/')
  const link = page.locator('.d-sidebar').getByRole('link', { name: '执行监控', exact: true })
  await expect(link).toHaveCount(1)
  await link.click()
  await expect(page).toHaveURL(/\/engine$/)
  await openEngine(page)
})

test('invariant 1: the user role has no entry and no snapshot request', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockEngineApi(page, { role: 'user' })
  await page.goto('/')
  await expect(
    page.locator('.d-sidebar').getByRole('link', { name: '执行监控', exact: true }),
  ).toHaveCount(0)

  await page.goto('/engine')
  await expect(page.getByText('无权访问')).toBeVisible()
  await expect(page.getByText('你的角色无权查看执行监控')).toBeVisible()
  expect(engineCalls(api), 'a denied page sends no engine request').toHaveLength(0)
})

test('invariant 2: the four metrics and the inflight rows render', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, { role: 'master' })
  await page.goto('/engine')
  await openEngine(page)

  const summary = page.locator('dl', { hasText: '待执行' })
  await expect(summary).toBeVisible()
  await expect(summary).toContainText('3')
  await expect(summary).toContainText('2')
  await expect(summary).toContainText('12')
  await expect(summary).toContainText('1')

  const inflight = page.locator('section.card', { has: page.getByRole('heading', { name: '在飞清单' }) })
  await expect(inflight).toContainText('138****8001')
  await expect(inflight).toContainText('合成学校甲')
  await expect(inflight).toContainText('138****8004')
  await expect(inflight).toContainText('1 分')
})

test('invariant 2: an empty inflight renders its own empty copy', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, {
    role: 'master',
    snapshot: { ...ENGINE_SNAPSHOT, inflight: [], retrying: [] },
  })
  await page.goto('/engine')
  await openEngine(page)

  await expect(page.getByText('当前没有在飞任务。')).toBeVisible()
  await expect(page.getByText('没有待重试的任务。')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('invariant 3: the lease remaining counts down while watching', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, { role: 'master' })
  await page.goto('/engine')
  await openEngine(page)

  const cell = page.locator('.engine-lease').first()
  const before = await cell.textContent()
  expect(before).toMatch(/分|秒/)
  await expect
    .poll(async () => cell.textContent(), { timeout: 5000, intervals: [250] })
    .not.toBe(before)
})

test('invariant 4: the retry list shows attempts, next run and result', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, { role: 'master' })
  await page.goto('/engine')
  await openEngine(page)

  const retryCard = page.locator('section.card', { has: page.getByRole('heading', { name: '待重试' }) })
  await expect(retryCard).toContainText('138****8003')
  await expect(retryCard).toContainText('早签到')
  await expect(retryCard).toContainText('2 次')
  await expect(retryCard).toContainText('transient')
  await expect(retryCard).toContainText('晚自习签到')
  await expect(retryCard).toContainText('rejected')
  // 同账号两条待重试都在：行身份是任务行，不是账号（表头加两数据行）。
  await expect(retryCard.getByRole('row')).toHaveCount(3)
})

test('invariant 4: a truncated retry list says so', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, {
    role: 'master',
    snapshot: { ...ENGINE_SNAPSHOT, retrying_truncated: true },
  })
  await page.goto('/engine')
  await openEngine(page)

  await expect(page.getByText('待重试超过 200 条，仅显示最先到期的 200 条。')).toBeVisible()
})

test('invariant 5: SSE business events land in the recent-events list', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, { role: 'master' })
  await page.goto('/engine')
  await openEngine(page)

  const eventsCard = page.locator('section.card', { has: page.getByRole('heading', { name: '最近事件' }) })
  await expect(eventsCard.getByText('还没有收到业务事件。')).toBeVisible()

  await fakeSourceOpen(page)
  await fakeSourceEmit(page, 'task.concluded')
  await fakeSourceEmit(page, 'task.progress')
  await expect(eventsCard.getByText('任务终态')).toBeVisible()
  await expect(eventsCard.getByText('任务重排')).toBeVisible()
})

test('invariant 5: a broken stream shows the banner and reconnects', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, { role: 'master' })
  await page.goto('/engine')
  await openEngine(page)

  await fakeSourceFail(page)
  const banner = page.locator('.run-live-banner')
  await expect(banner).toContainText('已停止实时')

  // 重连是换代：旧连接关闭，新连接建立。
  const before = await fakeSourceCount(page)
  await banner.getByRole('button', { name: '重连' }).click()
  await expect(banner).toHaveCount(0)
  expect(await fakeSourceCount(page)).toBeGreaterThan(before)
})

test('invariant 5: the disconnected page falls back to polling', async ({ page }) => {
  await installFakeEventSource(page)
  const api = await mockEngineApi(page, { role: 'master' })
  await page.goto('/engine')
  await openEngine(page)

  await fakeSourceFail(page)
  await expect(page.locator('.run-live-banner')).toBeVisible()
  const before = engineCalls(api).length
  // 断线降级间隔是 10 秒：给 12 秒观察一次轮询。
  await expect
    .poll(() => engineCalls(api).length, { timeout: 12_000, intervals: [1_000] })
    .toBeGreaterThan(before)
})

test('invariant 6: an error renders an alert, never the empty copy', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, {
    role: 'master',
    snapshotError: { status: 500, code: 'internal_error', message: '数据库不可用' },
  })
  await page.goto('/engine')

  await expect(page.getByRole('alert').filter({ hasText: '数据库不可用' })).toBeVisible()
  await expect(page.getByText('当前没有在飞任务。')).toHaveCount(0)
})

test('invariant 7: the monitor never says queue and fits 390px', async ({ page }) => {
  await installFakeEventSource(page)
  await mockEngineApi(page, { role: 'master' })
  await page.goto('/engine')
  await openEngine(page)

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
