import { test, expect } from '@playwright/test'
import { USER_ACCOUNT, mockUserAppApi } from './support/fake-user-app-api'

/**
 * 用户端七页行为契约。
 *
 * - 未登录落登录页并带回跳；
 * - <768 底栏、≥768 顶栏；两套导航互斥；
 * - user 只进用户端；admin/master 回管理台；
 * - 一人一号：成功、失败、待审核、无账号各有明确主文案；
 * - 今天 / 记录 / 我的 / 签到账号 / 添加账号 / 签到时间 / 修改密码
 *   七个路由都可直达，主要状态与保存动作有用户可见证据；
 * - 公告可关闭；390 无横向溢出，日历格与时间片保持可用触达。
 */

test.beforeEach(async ({ page }) => {
  // 夹具的“今天”固定为 2026-11-11；浏览器时钟必须同频，否则未来日与相对日期会错位。
  await page.clock.setFixedTime(new Date('2026-11-11T08:00:00+08:00'))
})

test('unauthenticated /app lands on login with redirect', async ({ page }) => {
  await mockUserAppApi(page, { role: null })
  await page.goto('/app')
  await expect(page).toHaveURL(/\/login\?redirect=%2Fapp$/)
})

test('portrait shows bottom tabs and reaches records', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/app')

  await expect(page.locator('.uapp-bottom')).toBeVisible()
  await expect(page.locator('.uapp-top-nav')).toBeHidden()
  await expect(page.locator('.uapp-tab.active')).toContainText('今天')

  await page.locator('.uapp-tab', { hasText: '记录' }).click()
  await expect(page).toHaveURL(/\/app\/history$/)
  await expect(page.locator('.uapp-tab.active')).toContainText('记录')
  await expect(page.getByRole('heading', { name: '签到记录' })).toBeVisible()
})

test('desktop shows top nav and reaches me', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/app/me')

  await expect(page.locator('.uapp-top-nav')).toBeVisible()
  await expect(page.locator('.uapp-bottom')).toBeHidden()
  await expect(page.locator('.uapp-top-link.active')).toContainText('我的')
  await expect(page.getByRole('heading', { name: '我的' })).toBeVisible()
})

test('admin visiting /app is sent back to the console', async ({ page }) => {
  await mockUserAppApi(page, { role: 'admin' })
  await page.goto('/app')
  await expect(page).toHaveURL('/')
})

test('today success shows the calm result, chosen time and 7-day strip', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/app')

  await expect(page.locator('.un-result__status')).toHaveText('今天已签到')
  await expect(page.locator('.un-result__clock')).toHaveText('07:02')
  await expect(page.locator('.uapp-hero-name')).toHaveText('东区宿舍')
  await expect(page.locator('.un-identity .un-chip')).toContainText('正常')
  await expect(page.locator('.un-week')).toContainText('近 7 天')
  await expect(page.locator('.un-ruler__range')).toContainText('06:30 – 07:50')
  await expect(page.locator('.un-ruler__callout.is-chosen')).toContainText('06:45')
  await expect(page.locator('.un-ruler__callout.is-actual')).toContainText('07:02')
  await expect(page.locator('.un-week__day.is-today')).toBeVisible()
  // 成功态不再给一个无意义的空操作区。
  await expect(page.locator('.uapp-hero-actions button')).toHaveCount(0)

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }))
  expect(overflow.scroll).toBe(overflow.inner)
})

test('today failure shows the retry action only when suggested', async ({ page }) => {
  await mockUserAppApi(page, {
    role: 'user',
    signStatus: [
      {
        account_id: USER_ACCOUNT.id,
        display_name: USER_ACCOUNT.display_name,
        phone_masked: USER_ACCOUNT.phone_masked,
        account_status: 'active',
        state: 'failed',
        run_at: '2026-11-11T07:12:00+08:00',
        stale: false,
      },
    ],
    diagnostics: [
      {
        account_id: USER_ACCOUNT.id,
        display_name: USER_ACCOUNT.display_name,
        phone_masked: USER_ACCOUNT.phone_masked,
        account_status: 'active',
        state: 'failed',
        run_at: '2026-11-11T07:12:00+08:00',
        attempts: 2,
        result: '',
        next_retry_at: '2026-11-11T07:20:00+08:00',
        suggestion: 'retry_now',
      },
    ],
  })
  await page.goto('/app')

  await expect(page.locator('.un-result__status')).toHaveText('今天没签上')
  await expect(page.locator('.uapp-hero-actions')).toContainText('再试一次')
  await expect(page.locator('.un-ruler__callout.is-actual')).toContainText('07:12')
})

test('today pending account shows review state and no retry', async ({ page }) => {
  await mockUserAppApi(page, {
    role: 'user',
    accounts: [{ ...USER_ACCOUNT, status: 'pending' }],
    signStatus: [],
    diagnostics: [],
  })
  await page.goto('/app')

  await expect(page.locator('.un-result__status')).toHaveText('账号正在审核')
  await expect(page.locator('.uapp-hero-actions')).not.toContainText('再试一次')
  await expect(page.locator('.un-ruler')).toHaveCount(0)
  await expect(page.getByRole('link', { name: '查看进度' })).toBeVisible()
})

test('today empty account shows create action', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user', accounts: [], signStatus: [], diagnostics: [] })
  await page.goto('/app')

  await expect(page.locator('.un-result__status')).toHaveText('还没有签到账号')
  await expect(page.getByRole('link', { name: '新建账号' })).toBeVisible()
})

test('announcement can be dismissed and its close target stays 44px', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user', announcement: '合成公告：窗口调整' })
  await page.goto('/app')

  const close = page.getByRole('button', { name: '关闭公告' })
  const box = await close.boundingBox()
  expect(box?.width).toBeGreaterThanOrEqual(44)
  expect(box?.height).toBeGreaterThanOrEqual(44)
  await expect(page.locator('.uapp-announcement')).toContainText('合成公告：窗口调整')
  await close.click()
  await expect(page.locator('.uapp-announcement')).toHaveCount(0)
})

test('user login lands on /app', async ({ page }) => {
  await mockUserAppApi(page, { role: null, loginRole: 'user' })
  await page.goto('/login')
  await page.locator('#login-email').fill('operator@example.com')
  await page.locator('#login-password').fill('not-a-real-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL('/app')
})

test('admin login keeps the console landing', async ({ page }) => {
  await mockUserAppApi(page, { role: null, loginRole: 'admin' })
  await page.goto('/login')
  await page.locator('#login-email').fill('operator@example.com')
  await page.locator('#login-password').fill('not-a-real-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL('/')
})

test('register tab shows the complete form and switches back', async ({ page }) => {
  await mockUserAppApi(page, { role: null })
  await page.goto('/register')

  await expect(page.locator('.uapp-seg-item.active')).toHaveText('注册')
  await expect(page.getByRole('heading', { name: '创建账号' })).toBeVisible()
  await expect(page.locator('#register-email')).toBeVisible()
  await expect(page.locator('#register-password')).toBeVisible()
  await expect(page.locator('#register-confirm')).toBeVisible()
  await expect(page.locator('#register-agree')).toBeVisible()

  await page.getByRole('tab', { name: '登录' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.locator('#login-email')).toBeVisible()
})

test('register closed state explains the switch', async ({ page }) => {
  await mockUserAppApi(page, { role: null, registrationEnabled: false })
  await page.goto('/register')
  await expect(page.getByText('当前未开放自助注册')).toBeVisible()
})

test('signing account page shows identity, device and controls', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/account')

  await expect(page.getByRole('heading', { name: '签到账号', level: 1 })).toBeVisible()
  await expect(page.locator('.un-account-hero__name')).toHaveText('东区宿舍')
  await expect(page.getByRole('heading', { name: '资料' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '设备' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '现场照片' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '控制' })).toBeVisible()
  await expect(page.locator('.un-row', { hasText: '暂停签到' })).toContainText('运行中')
  await expect(page.locator('.un-row', { hasText: '签到地点' })).toContainText('自动定位')
  await expect(page.locator('.uapp-top-link.active')).toContainText('我的')
})

test('new account detect-first reaches confirm, photo and pending', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user', accounts: [], signStatus: [], diagnostics: [] })
  await page.goto('/app/me/account/new')

  await expect(page.getByRole('heading', { name: '添加签到账号' })).toBeVisible()
  // 口令确实由后端加密持久化，页面不得再声称“不保存”。
  await expect(page.getByText('口令会加密保存', { exact: false })).toBeVisible()
  await page.locator('#uapp-new-phone').fill('13800138000')
  await page.locator('#uapp-new-password').fill('platform-pass')
  await page.getByRole('button', { name: '识别我的账号' }).click()

  await expect(page.getByRole('heading', { name: '确认账号资料' })).toBeVisible()
  await expect(page.getByText('合成大学', { exact: false })).toBeVisible()
  await expect(page.getByText('合成学校甲', { exact: false })).toBeVisible()

  await page.getByRole('button', { name: '继续' }).click()
  await expect(page.getByRole('heading', { name: '补充现场照片' })).toBeVisible()

  await page.getByRole('button', { name: '跳过照片，直接提交' }).click()
  await expect(page.getByRole('heading', { name: '已提交审核' })).toBeVisible()
  await expect(page.getByText('资料已经在审核队列里')).toBeVisible()
})

test('records deep link shows the month and day detail', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/history?day=2026-11-10')

  await expect(page.getByRole('heading', { name: '签到记录' })).toBeVisible()
  await expect(page.locator('.un-cal__month')).toContainText('2026年11月')
  await expect(page.locator('.un-day-card')).toContainText('11月10日 星期二')
  await expect(page.locator('.un-day-card')).toContainText('东区宿舍')
  await expect(page.locator('.un-day-card')).toContainText('成功')

  await page.getByRole('button', { name: /11月9日/ }).click()
  await expect(page).toHaveURL(/day=2026-11-09/)
})

test('me shows account facts, preferences and toggles failure mail', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me')

  await expect(page.getByRole('heading', { name: '我的' })).toBeVisible()
  await expect(page.locator('.un-profile__facts')).toContainText('06:30–07:50')
  await expect(page.locator('.un-profile__facts')).toContainText('06:45')
  await expect(page.locator('.un-me__week-mark')).toHaveCount(30)

  const toggle = page.getByRole('switch', { name: '失败邮件' })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')

  await page.getByLabel('照片保留期').selectOption('180')
  await expect(page.getByLabel('照片保留期')).toHaveValue('180')
})

test('me reaches time preference and password pages', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me')

  await page.getByRole('link', { name: /调整/ }).click()
  await expect(page).toHaveURL(/\/app\/me\/time$/)
  await expect(page.getByRole('heading', { name: '签到时间' })).toBeVisible()

  await page.getByRole('link', { name: '返回我的' }).click()
  await page.getByRole('link', { name: /登录密码/ }).click()
  await expect(page).toHaveURL(/\/app\/me\/password$/)
  await expect(page.locator('#un-pw-current')).toBeVisible()
})

test('me deletes the login account with the site password', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me')

  await page.locator('.un-security__item--danger').click()
  await page.locator('#uapp-reauth-password').fill('not-a-real-password')
  const request = page.waitForRequest(
    (req) => req.url().endsWith('/api/me/delete') && req.method() === 'POST',
  )
  await page.getByRole('dialog').getByRole('button', { name: '删除登录账号' }).click()
  const body = (await request).postDataJSON() as { current_password?: string }
  expect(body.current_password).toBe('not-a-real-password')
  await expect(page).toHaveURL(/\/login$/)
})

test('time preference saves a slot and shows the absolute time', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/time')

  await expect(page.locator('.un-time__value')).toHaveText('06:45')
  await page.getByRole('option', { name: '06:50' }).click()
  await page.getByRole('button', { name: '保存时间偏好' }).click()
  await expect(page.locator('.un-time__value')).toHaveText('06:50')
  await expect(page).toHaveURL(/\/app\/me\/time$/)
})

test('time preference restores automatic allocation', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/time')

  await page.getByRole('button', { name: '恢复自动分配' }).click()
  await expect(page.locator('.un-time__value')).toHaveText('自动分配')
})

test('time preference skips disabled leading slot for keyboard users', async ({ page }) => {
  await mockUserAppApi(page, {
    role: 'user',
    timePref: {
      offset_min: null,
      allowed: true,
      window: { start: '06:30', end: '07:50' },
      slots: [
        { offset_min: 0, label: '06:30', pct: 100, disabled: true, reason: '已满', edge_note: '' },
        { offset_min: 5, label: '06:35', pct: 20, disabled: false, edge_note: '' },
      ],
      estimated: null,
      estimate_note: '',
    },
  })
  await page.goto('/app/me/time')

  const options = page.locator('[role="option"]')
  await expect(options.nth(0)).toBeDisabled()
  await expect(options.nth(1)).toBeEnabled()
  await options.nth(1).focus()
  await expect(options.nth(1)).toBeFocused()
})

test('failure mail switch round-trips without sticking', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me')

  const toggle = page.getByRole('switch', { name: '失败邮件' })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await expect(toggle).toBeEnabled()
})

test('password page enforces the server policy', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/password')

  await page.locator('#un-pw-current').fill('not-a-real-password')
  await page.locator('#un-pw-next').fill('short')
  await page.locator('#un-pw-confirm').fill('short')
  await page.getByRole('button', { name: '保存新密码' }).click()
  await expect(page.getByText('新密码不符合规则')).toBeVisible()
  await expect(page.getByRole('heading', { name: '修改密码' })).toBeVisible()
})

test('password page reveals fields without leaving the form', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/password')

  await page.getByRole('button', { name: '显示当前密码' }).click()
  await expect(page.locator('#un-pw-current')).toHaveAttribute('type', 'text')
  await expect(page.getByRole('button', { name: '隐藏当前密码' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
})

test('delete signing account and refresh device send the site password', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/account')

  await page.locator('.un-row--danger', { hasText: '删除签到账号' }).click()
  await page.locator('#uapp-reauth-password').fill('not-a-real-password')
  const deleteRequest = page.waitForRequest(
    (req) =>
      req.url().endsWith(`/api/me/accounts/${USER_ACCOUNT.id}/delete`) &&
      req.method() === 'POST',
  )
  await page.getByRole('dialog').getByRole('button', { name: '删除签到账号' }).click()
  const deleteBody = (await deleteRequest).postDataJSON() as {
    current_password?: string
  }
  expect(deleteBody.current_password).toBe('not-a-real-password')

  await page.goto('/app/me/account')
  await page.getByRole('button', { name: /^刷新设备/ }).click()
  await page.locator('#uapp-reauth-password').fill('not-a-real-password')
  const refreshRequest = page.waitForRequest(
    (req) =>
      req.url().endsWith(`/api/accounts/${USER_ACCOUNT.id}/device-refresh`) &&
      req.method() === 'POST',
  )
  await page.getByRole('dialog').getByRole('button', { name: '刷新设备' }).click()
  const refreshBody = (await refreshRequest).postDataJSON() as {
    current_password?: string
  }
  expect(refreshBody.current_password).toBe('not-a-real-password')
})

test('credential edit sends current_password and expected_updated_at', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/account')

  await page.locator('.un-row', { hasText: '易班资料' }).click()
  await page.locator('#uapp-edit-password').fill('new-platform-pass')
  await page.locator('#uapp-edit-current').fill('not-a-real-password')

  const request = page.waitForRequest(
    (req) =>
      req.url().endsWith(`/api/accounts/${USER_ACCOUNT.id}`) &&
      req.method() === 'PATCH',
  )
  await page.getByRole('dialog').getByRole('button', { name: '保存' }).click()
  const body = (await request).postDataJSON() as {
    current_password?: string
    expected_updated_at?: string
    password?: string
  }
  expect(body.current_password).toBe('not-a-real-password')
  expect(body.password).toBe('new-platform-pass')
  expect(body.expected_updated_at).toBe(USER_ACCOUNT.updated_at)
})

test('remark-only edit in the credentials sheet does not ask for the site password', async ({
  page,
}) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/account')

  await page.locator('.un-row', { hasText: '易班资料' }).click()
  await page.locator('#uapp-edit-name').fill('新备注')

  const request = page.waitForRequest(
    (req) =>
      req.url().endsWith(`/api/accounts/${USER_ACCOUNT.id}`) &&
      req.method() === 'PATCH',
  )
  await page.getByRole('dialog').getByRole('button', { name: '保存' }).click()
  const body = (await request).postDataJSON() as Record<string, unknown>
  expect(body.name).toBe('新备注')
  expect(body.current_password).toBeUndefined()
  expect(body.password).toBeUndefined()
  expect(body.phone).toBeUndefined()
  expect(body.phone_model).toBeUndefined()
})

test('no-op save in the credentials sheet sends nothing', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/me/account')

  let patched = 0
  page.on('request', (req) => {
    if (req.url().endsWith(`/api/accounts/${USER_ACCOUNT.id}`) && req.method() === 'PATCH') {
      patched += 1
    }
  })

  await page.locator('.un-row', { hasText: '易班资料' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存' }).click()
  await expect(page.getByRole('dialog')).toContainText('没有需要保存的修改。')
  expect(patched).toBe(0)
})

test('restore deleted signing account sends the site password', async ({ page }) => {
  await mockUserAppApi(page, {
    role: 'user',
    accounts: [{ ...USER_ACCOUNT, deleted_at: '2026-11-10T00:00:00Z' }],
    signStatus: [],
    diagnostics: [],
  })
  await page.goto('/app/me/account')

  await expect(page.locator('.un-chip', { hasText: '已删除' })).toBeVisible()
  await page.locator('.un-row--danger', { hasText: '恢复签到账号' }).click()
  await page.locator('#uapp-reauth-password').fill('not-a-real-password')
  const request = page.waitForRequest(
    (req) =>
      req.url().endsWith(`/api/me/accounts/${USER_ACCOUNT.id}/restore`) &&
      req.method() === 'POST',
  )
  await page.getByRole('dialog').getByRole('button', { name: '恢复签到账号' }).click()
  const body = (await request).postDataJSON() as { current_password?: string }
  expect(body.current_password).toBe('not-a-real-password')
})

test('login on a soft-deleted account auto-restores and retries', async ({ page }) => {
  await mockUserAppApi(page, { role: null, loginRole: 'user', loginFailFirst: true })
  await page.goto('/login')

  const seen: string[] = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (path === '/api/auth/login' || path === '/api/me/restore') seen.push(path)
  })

  await page.locator('#login-email').fill('operator@example.com')
  await page.locator('#login-password').fill('not-a-real-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()

  await expect(page).toHaveURL(/\/app$/)
  expect(seen).toEqual(['/api/auth/login', '/api/me/restore', '/api/auth/login'])
})

test('auto-restore failure keeps the generic credential error', async ({ page }) => {
  await mockUserAppApi(page, { role: null, loginFailFirst: true })
  await page.route('**/api/me/restore', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'unauthorized', message: 'invalid email or password' }),
    }),
  )
  await page.goto('/login')

  await page.locator('#login-email').fill('operator@example.com')
  await page.locator('#login-password').fill('wrong-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()

  await expect(page.getByRole('alert')).toContainText('邮箱或密码错误')
  await expect(page).toHaveURL(/\/login$/)
})

test('user-app UX invariants: skip link, 44px targets, calendar cell, keyboard time slots', async ({
  page,
}) => {
  await mockUserAppApi(page, { role: 'user', announcement: '维护通知' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/app')

  await expect(async () => {
    await page.keyboard.press('Tab')
    await expect(page.locator('.uapp-skip')).toBeFocused({ timeout: 1_000 })
  }).toPass({ timeout: 10_000 })

  const closeBox = await page.locator('.uapp-announcement-close').boundingBox()
  expect(closeBox?.width).toBeGreaterThanOrEqual(44)
  expect(closeBox?.height).toBeGreaterThanOrEqual(44)

  await page.goto('/app/history?day=2026-11-10')
  await expect(page.locator('.un-cal__cell').first()).toBeVisible()
  const cellBox = await page.evaluate(() => {
    const cell = document.querySelector('.un-cal__cell')
    return cell ? cell.getBoundingClientRect() : null
  })
  expect(cellBox?.width).toBeGreaterThanOrEqual(44)
  expect(cellBox?.height).toBeGreaterThanOrEqual(44)
  await expect(page.locator('.un-cal__cell[aria-pressed="true"]')).toBeVisible()

  await page.goto('/app/me/time')
  await expect(page.locator('.un-time__slot').first()).toBeVisible()
  const tickBox = await page.evaluate(() => {
    const tick = document.querySelector('.un-time__slot')
    return tick ? tick.getBoundingClientRect() : null
  })
  expect(tickBox?.width).toBeGreaterThanOrEqual(44)
  const selected = page.locator('.un-time__slot[aria-selected="true"]')
  await selected.focus()
  await selected.press('ArrowRight')
  await expect(page.locator('.un-time__slot[aria-label="06:50"]')).toBeFocused()
})

test('password page clears must_change_password and handles session expiry', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user', mustChangePassword: true })
  await page.goto('/app/me/password')
  await expect(page.getByText('当前账号需要先完成密码修改，才能继续使用。')).toBeVisible()

  await page.locator('#un-pw-current').fill('OldPass123!')
  await page.locator('#un-pw-next').fill('NewStrong1!')
  await page.locator('#un-pw-confirm').fill('NewStrong1!')
  await page.getByRole('button', { name: '保存新密码' }).click()

  await expect(page).toHaveURL(/\/app\/me$/)
  await expect(page.getByText('当前账号需要先完成密码修改，才能继续使用。')).toHaveCount(0)

  await page.goto('/app/me/password')
  await expect(page.locator('#un-pw-current')).toBeVisible()
  await page.unroute('**/api/auth/change-password')
  await page.route('**/api/auth/change-password', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'UNAUTHORIZED', message: 'expired' } }),
    }),
  )
  await page.route('**/api/auth/me', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'UNAUTHORIZED', message: 'expired' } }),
    }),
  )
  await page.locator('#un-pw-current').fill('OldPass123!')
  await page.locator('#un-pw-next').fill('NewStrong2!')
  await page.locator('#un-pw-confirm').fill('NewStrong2!')
  await page.getByRole('button', { name: '保存新密码' }).click()

  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByText('会话已过期，请重新登录')).toBeVisible()
})

test('new account manual school switch validates phone and password first', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user', accounts: [], signStatus: [], diagnostics: [] })
  await page.goto('/app/me/account/new')

  await page.getByRole('button', { name: '手动选择学校' }).click()
  await expect(page.getByRole('alert')).toContainText('11 位易班手机号')
  await expect(page.locator('#uapp-new-phone')).toBeFocused()
  await expect(page.getByRole('heading', { name: '添加签到账号' })).toBeVisible()
})
