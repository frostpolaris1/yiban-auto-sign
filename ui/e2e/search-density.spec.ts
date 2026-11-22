import { test, expect, type Page } from '@playwright/test'
import { mockAccountsApi, ACCOUNTS, type FakeApi } from './support/fake-accounts-api'
import { mockDashboardApi } from './support/fake-dashboard-api'

/**
 * 任务书 06：顶栏全局搜索 + 密度切换。
 * 断言用户可见不变式：搜得到、跳得到、按角色收敛、密度可持久化。
 * user 角色进不了管理台壳（一律 302 到用户端 `/app`），故顶栏搜索只对管理角色存在。
 * 防抖 250ms 用「最终只发一次带 q 的请求」来钉，不钉计时器本身。
 */

/** 假用户端点的最小行。形状取契约 UserPage；邮箱为保留域合成值。 */
const SEARCH_USERS = [
  {
    id: 'user-op',
    email: 'operator@example.com',
    role: 'master',
    status: 'active',
    must_change_password: false,
    account_count: 2,
    pending_count: 1,
    review_count: 0,
    created_at: '2026-10-01T09:00:00Z',
    last_login_at: null,
    deleted_at: null,
  },
  {
    id: 'user-mb',
    email: 'member-one@example.com',
    role: 'user',
    status: 'active',
    must_change_password: false,
    account_count: 1,
    pending_count: 0,
    review_count: 0,
    created_at: '2026-10-02T09:00:00Z',
    last_login_at: null,
    deleted_at: null,
  },
]

/** 假任务端点的最小行。形状取契约 TaskPage。 */
const SEARCH_TASKS = [
  {
    id: 'task-1',
    account_id: 'acc-1',
    account_phone_masked: '138****8000',
    school_id: 'school-a',
    school_name: '合成学校甲',
    task_name: '早签到',
    state: 'success',
    attempts: 1,
    result: 'ok',
    run_at: '2026-11-19T08:10:00Z',
    claimed_at: null,
    updated_at: '2026-11-19T08:12:00Z',
  },
  {
    id: 'task-2',
    account_id: 'acc-2',
    account_phone_masked: '139****9000',
    school_id: 'school-b',
    school_name: '合成学校乙',
    task_name: '晚自习签到',
    state: 'failed',
    attempts: 3,
    result: 'credential_invalid',
    run_at: '2026-11-19T09:10:00Z',
    claimed_at: null,
    updated_at: '2026-11-19T09:20:00Z',
  },
]

type SearchFixture = {
  /** 全部 `/api/users` 与 `/api/admin/tasks` 的发起记录（含被角色门挡下时应为空的核对）。 */
  paths: string[]
  accounts: FakeApi
}

/**
 * 用户与任务两个假端点：与服务端同口径的极简筛选。
 * `/api/users?q=` 按邮箱前缀；`/api/admin/tasks?q=` 按任务/学校名子串。
 * 角色门同契约：users 只归 master；tasks 归 master/admin。
 */
async function mockSearchEndpoints(
  page: Page,
  role: 'master' | 'admin' | 'user',
  extraTasks: typeof SEARCH_TASKS = [],
): Promise<SearchFixture> {
  const tasks = [...SEARCH_TASKS, ...extraTasks]
  const paths: string[] = []
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname
    if (pathname === '/api/users' || pathname === '/api/admin/tasks') paths.push(pathname)
  })

  await page.route(
    (url) => url.pathname === '/api/users',
    async (route) => {
      const request = route.request()
      if (role !== 'master') {
        await route.fulfill({
          status: 403,
          contentType: 'application/json',
          body: JSON.stringify({ error: { code: 'forbidden', message: '仅 master 可查看用户列表' } }),
        })
        return
      }
      const q = new URL(request.url()).searchParams.get('q') ?? ''
      const items = q === '' ? SEARCH_USERS : SEARCH_USERS.filter((u) => u.email.startsWith(q))
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          next_cursor: '',
          counts: { pending: 1, normal: 2, vacant: 0, deleted: 0 },
        }),
      })
    },
  )

  await page.route(
    (url) => url.pathname === '/api/admin/tasks',
    async (route) => {
      const request = route.request()
      if (role !== 'master' && role !== 'admin') {
        await route.fulfill({
          status: 403,
          contentType: 'application/json',
          body: JSON.stringify({ error: { code: 'forbidden', message: '仅 master/admin 可查看运行页' } }),
        })
        return
      }
      const q = new URL(request.url()).searchParams.get('q') ?? ''
      const items =
        q === ''
          ? tasks
          : tasks.filter((t) => t.task_name.includes(q) || t.school_name.includes(q))
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items, next_cursor: '' }),
      })
    },
  )

  return { paths, accounts: {} as FakeApi }
}

async function openSearch(page: Page): Promise<void> {
  // 先等搜索触发器挂载（壳就绪），再按 Ctrl+K：
  // 全量并跑时按键可能早于监听器注册，会静默丢失。
  await expect(page.locator('.d-topbar .cmd')).toBeVisible()
  await page.keyboard.press('Control+k')
  await expect(page.getByRole('dialog', { name: '全局搜索' })).toBeVisible()
}

async function activeRole(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.getAttribute('role') ?? null)
}

async function searchInput(page: Page) {
  return page.getByRole('dialog', { name: '全局搜索' }).getByRole('combobox')
}

async function typeQuery(page: Page, term: string): Promise<void> {
  await (await searchInput(page)).pressSequentially(term, { delay: 10 })
}

test.describe('global search', () => {
  test('Ctrl+K opens the panel and focuses the input', async ({ page }) => {
    await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    await page.goto('/design')

    await openSearch(page)
    expect(await activeRole(page), 'the input takes focus on open').toBe('combobox')
  })

  test('a masked phone query finds the account and Enter lands on /accounts with the row marked exact', async ({
    page,
  }) => {
    const accounts = await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    await page.goto('/design')

    await openSearch(page)
    await typeQuery(page, '138****8000')
    const option = page
      .getByRole('dialog', { name: '全局搜索' })
      .getByRole('option', { name: /138\*\*\*\*8000/ })
    await expect(option).toBeVisible()

    // 防抖 250ms：连续输入只收敛成一次带 q 的账号请求，且掩码号按后 4 位直达。
    await expect.poll(() => qAccountCalls(accounts)).toEqual(['8000'])

    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/accounts/)
    await expect(page.locator('tr[data-exact-match="true"]')).toContainText('138****8000')
  })

  test('admin sees no user group and issues no /api/users request', async ({ page }) => {
    await mockAccountsApi(page, { role: 'admin' })
    const fixture = await mockSearchEndpoints(page, 'admin')
    await page.goto('/design')

    await openSearch(page)
    await typeQuery(page, '签到')
    await expect(
      page.getByRole('dialog', { name: '全局搜索' }).getByRole('option', { name: /早签到/ }),
    ).toBeVisible()
    await expect(
      page.getByRole('dialog', { name: '全局搜索' }).getByText('用户', { exact: true }),
    ).toHaveCount(0)
    await expect.poll(() => fixture.paths).toEqual(['/api/admin/tasks'])
  })

  test('master gets user results and Enter lands on /users with the query', async ({ page }) => {
    await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    await page.goto('/design')

    await openSearch(page)
    await typeQuery(page, 'operator')
    await expect(
      page.getByRole('dialog', { name: '全局搜索' }).getByRole('option', {
        name: /operator@example\.com/,
      }),
    ).toBeVisible()

    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/users\?q=operator/)
    await expect(page.getByRole('table', { name: '用户列表' })).toContainText('operator@example.com')
  })

  test('the task result jumps to the ledger with q kept', async ({ page }) => {
    await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    await page.goto('/design')

    await openSearch(page)
    await typeQuery(page, '签到')
    const taskOption = page
      .getByRole('dialog', { name: '全局搜索' })
      .getByRole('option', { name: /晚自习签到/ })
    await expect(taskOption).toBeVisible()
    await page.keyboard.press('Enter')
    // 台账已随 #380 迁到 `/logs?tab=events`（`/run` 已 302）。分支原本断言 `/run?q=`，
    // 重放到新 develop 后改为台账页签；`/run` 的重定向会丢掉页签语义。
    await expect(page).toHaveURL(/\/logs\?tab=events&q=%E7%AD%BE%E5%88%B0|\/logs\?tab=events&q=签到/)
  })

  test('arrow keys cycle options and Enter opens the active one', async ({ page }) => {
    await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    await page.goto('/design')

    await openSearch(page)
    // 单位数字前缀在服务端口径下命中全部三条账号（1 开头）。
    await typeQuery(page, '1')
    const options = page.getByRole('dialog', { name: '全局搜索' }).getByRole('option')
    await expect(options).toHaveCount(3)
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('ArrowDown')
    await expect(options.nth(2)).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/accounts/)
    await expect(page.locator('tr[data-exact-match="true"]')).toContainText('135****0001')
  })

  test('no result shows the example hint; Escape closes and returns focus to the trigger', async ({
    page,
  }) => {
    await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    await page.goto('/design')

    await openSearch(page)
    await typeQuery(page, '不存在的东西')
    const dialog = page.getByRole('dialog', { name: '全局搜索' })
    await expect(dialog.getByText('没有匹配结果')).toBeVisible()
    await expect(dialog.getByText(/138\*\*\*\*8000/)).toBeVisible()
    await expect(
      dialog.getByRole('combobox'),
      'the combobox stays expanded while the popup is open, hits or not',
    ).toHaveAttribute('aria-expanded', 'true')

    // Tab / Shift+Tab 锁定在自绘搜索弹层内，不逃出到背景页面。
    await page.keyboard.press('Tab')
    expect(await activeRole(page), 'Tab stays trapped in combobox').toBe('combobox')
    await page.keyboard.press('Shift+Tab')
    expect(await activeRole(page), 'Shift+Tab stays trapped in combobox').toBe('combobox')

    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    const focused = await page.evaluate(() => document.activeElement?.className ?? '')
    expect(focused, 'focus returns to the topbar search button').toContain('cmd')
  })

  test('clearing the input while a query is in flight drops the stale response', async ({
    page,
  }) => {
    await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    // 慢响应：只拖住带 q 的账号检索，其余请求照常放行。
    await page.route(
      (url) => url.pathname === '/api/accounts' && url.searchParams.has('q'),
      async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 600))
        await route.fallback()
      },
    )
    await page.goto('/design')

    await openSearch(page)
    const requestSeen = page.waitForRequest((request) => {
      const url = new URL(request.url())
      return url.pathname === '/api/accounts' && url.searchParams.get('q') === '138'
    })
    await typeQuery(page, '138')
    await requestSeen

    // 旧响应还在延迟窗口里，清空输入让序号作废。
    await (await searchInput(page)).fill('')
    const dialog = page.getByRole('dialog', { name: '全局搜索' })
    await expect(dialog.getByText(/输入即搜/)).toBeVisible()

    // 等过延迟窗口再断言：旧响应落地也不得把结果写回面板。
    await page.waitForTimeout(800)
    await expect(dialog.getByRole('option')).toHaveCount(0)
    await expect(dialog.getByText('东区宿舍')).toHaveCount(0)
  })

  test('user role is sent to /app and never gets the manager search', async ({ page }) => {
    const accounts = await mockAccountsApi(page, { role: 'user' })
    const fixture = await mockSearchEndpoints(page, 'user')
    await page.goto('/design')

    await expect(page).toHaveURL(/\/app$/)
    await expect(page.locator('.uapp-bar'), 'the user app takes over').toBeVisible()
    await expect(page.locator('.d-sidebar'), 'the manager shell never renders').toHaveCount(0)
    await expect(page.locator('.d-topbar .cmd'), 'no global search trigger').toHaveCount(0)

    expect(fixture.paths, 'no role-gated search endpoint is called').toEqual([])
    expect(qAccountCalls(accounts), 'no account query is issued').toEqual([])
  })
})

test.describe('density', () => {
  test('standard is the default geometry; compact shrinks rows and card padding, persists, and survives reload', async ({
    page,
  }) => {
    await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    await page.goto('/accounts')

    await expect(page.locator('html')).toHaveAttribute('data-density', 'standard')
    const row = page.getByRole('row').filter({ has: page.getByRole('button', { name: '查看 138****8000' }) })
    await expect
      .poll(async () => {
        const box = await row.boundingBox()
        return box === null ? null : Math.round(box.height)
      })
      .toBe(48)

    await page.getByRole('button', { name: /密度/ }).click()
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')
    await expect
      .poll(async () => {
        const box = await row.boundingBox()
        return box === null ? null : Math.round(box.height)
      })
      .toBe(40)
    await expect(page.locator('.card').first()).toHaveCSS('padding-top', '14px')
    expect(
      await page.evaluate(() => window.localStorage.getItem('dash26-density')),
    ).toBe('compact')

    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')
    await expect
      .poll(async () => {
        const box = await row.boundingBox()
        return box === null ? null : Math.round(box.height)
      })
      .toBe(40)
  })

  test('KPI font size is identical in both densities', async ({ page }) => {
    await mockDashboardApi(page, { role: 'master' })
    await page.goto('/')
    const before = await page.locator('.kpi-value').first().evaluate((el) => getComputedStyle(el).fontSize)
    await page.getByRole('button', { name: /密度/ }).click()
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')
    const after = await page.locator('.kpi-value').first().evaluate((el) => getComputedStyle(el).fontSize)
    expect(after, 'compact must not shrink KPI type').toBe(before)
    await page.getByRole('button', { name: /密度/ }).click()
    const back = await page.locator('.kpi-value').first().evaluate((el) => getComputedStyle(el).fontSize)
    expect(back, 'switching back restores the standard geometry').toBe(before)
  })

  test('a session that never toggles writes no density key', async ({ page }) => {
    await mockAccountsApi(page, { role: 'master' })
    await mockSearchEndpoints(page, 'master')
    await page.goto('/design')
    expect(
      await page.evaluate(() => window.localStorage.getItem('dash26-density')),
      'density persists only after an explicit toggle',
    ).toBeNull()
  })
})

test.describe('mobile', () => {
  test('at 390 the search trigger and density toggle hit at least 44px and the open panel does not overflow', async ({
    page,
  }) => {
    // 给「限高＋内部滚动」造足量结果：账号侧 11 条同词、任务侧 3 条同词。
    const overflowAccounts = Array.from({ length: 11 }, (_, index) => ({
      ...ACCOUNTS[2],
      id: `acc-overflow-${index}`,
      display_name: '签到账号0001',
    }))
    const overflowTasks = ['午间签到', '夜间签到', '课前签到'].map((taskName, index) => ({
      ...SEARCH_TASKS[0],
      id: `task-overflow-${index}`,
      task_name: taskName,
    }))
    await mockAccountsApi(page, {
      role: 'master',
      accounts: [...ACCOUNTS, ...overflowAccounts],
    })
    await mockSearchEndpoints(page, 'master', overflowTasks)
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/design')

    const trigger = page.locator('.d-topbar .cmd')
    const density = page.getByRole('button', { name: /密度/ })
    const triggerBox = await trigger.boundingBox()
    const densityBox = await density.boundingBox()
    expect(triggerBox && Math.min(triggerBox.width, triggerBox.height)).toBeGreaterThanOrEqual(44)
    expect(densityBox && Math.min(densityBox.width, densityBox.height)).toBeGreaterThanOrEqual(44)

    await openSearch(page)
    await typeQuery(page, '138')
    await expect(
      page.getByRole('dialog', { name: '全局搜索' }).getByRole('option', { name: /138\*\*\*\*8000/ }),
    ).toBeVisible()
    const optionBox = await page
      .getByRole('dialog', { name: '全局搜索' })
      .getByRole('option')
      .first()
      .boundingBox()
    expect(optionBox && optionBox.height).toBeGreaterThanOrEqual(44)

    // 面板限高与内部滚动：10 条结果（每组上限 5）超出 480px，末项必须可达。
    await (await searchInput(page)).fill('签到')
    const panel = page.getByRole('dialog', { name: '全局搜索' })
    await expect(panel.getByRole('option')).toHaveCount(10)
    const panelBox = await panel.boundingBox()
    expect(panelBox && panelBox.height).toBeLessThanOrEqual(480)
    const scroll = await panel.evaluate((element) => ({
      overflowY: getComputedStyle(element).overflowY,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
    }))
    expect(scroll.overflowY).toBe('auto')
    expect(scroll.scrollHeight, 'the capped panel must own the overflow').toBeGreaterThan(
      scroll.clientHeight,
    )
    const lastOption = panel.getByRole('option').last()
    await lastOption.scrollIntoViewIfNeeded()
    await expect(lastOption).toBeVisible()
    const lastBox = await lastOption.boundingBox()
    expect(lastBox && lastBox.y + lastBox.height).toBeLessThanOrEqual(844)

    const metrics = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }))
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1)
  })
})

/** 只数带 q 参数的账号列表请求：页面自身的裸列表请求不算。 */
function qAccountCalls(api: FakeApi): string[] {
  return api.calls
    .filter((call) => call.method === 'GET' && call.path === '/api/accounts' && call.params.q !== undefined)
    .map((call) => call.params.q as string)
}
