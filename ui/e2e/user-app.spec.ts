import { test, expect } from '@playwright/test'
import { USER_ACCOUNT, mockUserAppApi } from './support/fake-user-app-api'

/**
 * 用户端壳与首页不变式。
 *
 * - 未登录落登录页并带回跳；
 * - <768 底栏、≥768 顶栏；两套导航互斥；
 * - user 只进用户端；admin/master 回管理台；
 * - 一人一号：成功、失败、待审核、无账号各有明确主文案；
 * - 公告可关闭；7 天条有数据才出现；
 * - 390 无横向溢出。
 */

test('unauthenticated /app lands on login with redirect', async ({ page }) => {
  await mockUserAppApi(page, { role: null })
  await page.goto('/app')
  await expect(page).toHaveURL(/\/login\?redirect=%2Fapp$/)
})

test('portrait shows bottom tabs and reaches history', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/app')

  await expect(page.locator('.uapp-bottom')).toBeVisible()
  await expect(page.locator('.uapp-top-nav')).toBeHidden()
  await expect(page.locator('.uapp-tab.active')).toContainText('主页')

  await page.locator('.uapp-tab', { hasText: '历史' }).click()
  await expect(page).toHaveURL(/\/app\/history$/)
  await expect(page.locator('.uapp-tab.active')).toContainText('历史')
  await expect(page.locator('.uapp-greeting')).toHaveText('历史')
})

test('desktop shows top nav and reaches settings', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/app/settings')

  await expect(page.locator('.uapp-top-nav')).toBeVisible()
  await expect(page.locator('.uapp-bottom')).toBeHidden()
  await expect(page.locator('.uapp-top-link.active')).toContainText('设置')
  await expect(page.locator('.uapp-greeting')).toHaveText('设置')
})

test('admin visiting /app is sent back to the console', async ({ page }) => {
  await mockUserAppApi(page, { role: 'admin' })
  await page.goto('/app')
  await expect(page).toHaveURL('/')
})

test('home success shows the hero, big time and 7-day strip', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/app')

  await expect(page.locator('.uapp-greeting')).toHaveText(/早上好|下午好|晚上好|夜深了/)
  await expect(page.locator('.uapp-hero-name')).toHaveText('东区宿舍')
  await expect(page.locator('.uapp-hero-time')).toHaveText('7:02')
  await expect(page.locator('.uapp-hero-title')).toHaveText('今天签到成功')
  await expect(page.locator('.uapp-status')).toContainText('正常')
  await expect(page.locator('.uapp-week')).toContainText('近 7 天')
  // 历史入口只留 Hero 脚部那一个（周卡不再挂第二个「查看历史」）。
  await expect(page.locator('.uapp-hero-actions')).toContainText('查看记录')
  await expect(page.locator('.uapp-week-link')).toHaveCount(0)
  await expect(page.locator('.uapp-window')).toContainText('可签到窗口')
  await expect(page.locator('.uapp-window')).toContainText('06:30 – 07:50')
  await expect(page.locator('.uapp-window-scale')).toContainText('07:00')
  await expect(page.locator('.uapp-window-legend')).toContainText('你选 06:45')
  await expect(page.locator('.uapp-window-legend')).toContainText('实际 07:02')
  await expect(page.locator('.uapp-day-cell--today')).toBeVisible()
  await expect(page.locator('.uapp-hero-actions button')).toHaveCount(1)

  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }))
  expect(overflow.scroll).toBe(overflow.inner)
})

test('home failure shows the retry action only when suggested', async ({ page }) => {
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

  await expect(page.locator('.uapp-hero-title')).toHaveText('今天没签上')
  await expect(page.locator('.uapp-hero-actions')).toContainText('再试一次')
  await expect(page.locator('.uapp-next-time')).toContainText('下次重试')
})

test('home pending account shows review state and no retry', async ({ page }) => {
  await mockUserAppApi(page, {
    role: 'user',
    accounts: [{ ...USER_ACCOUNT, status: 'pending' }],
    signStatus: [],
    diagnostics: [],
  })
  await page.goto('/app')

  await expect(page.locator('.uapp-hero-title')).toHaveText('账号正在审核')
  await expect(page.locator('.uapp-hero-actions')).not.toContainText('再试一次')
  await expect(page.locator('.uapp-window')).toHaveCount(0)
})

test('home empty account shows create action', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user', accounts: [], signStatus: [], diagnostics: [] })
  await page.goto('/app')

  await expect(page.locator('.uapp-hero-title')).toHaveText('还没有签到账号')
  await expect(page.locator('.uapp-hero-actions')).toContainText('新建账号')
})

test('announcement can be dismissed', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user', announcement: '合成公告：窗口调整' })
  await page.goto('/app')

  await expect(page.locator('.uapp-announcement')).toContainText('合成公告：窗口调整')
  await page.getByRole('button', { name: '关闭公告' }).click()
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

test('account page shows identity, device and controls', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/account')

  await expect(page.locator('.uapp-hero-name')).toHaveText('东区宿舍')
  await expect(page.locator('.uapp-card-title', { hasText: '账号资料' })).toBeVisible()
  await expect(page.locator('.uapp-card-title', { hasText: '设备' })).toBeVisible()
  await expect(page.locator('.uapp-card-title', { hasText: '现场照片' })).toBeVisible()
  await expect(page.getByText('暂停签到')).toBeVisible()
  await expect(page.getByText('自动定位')).toBeVisible()
  await expect(page.locator('.uapp-tab.active')).toContainText('主页')
})

test('new account detect-first reaches confirm, photo and pending', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/account/new')

  await page.locator('#uapp-new-phone').fill('13800138000')
  await page.locator('#uapp-new-password').fill('platform-pass')
  await page.getByRole('button', { name: '识别我的账号' }).click()

  // 向导是 hub 之上的模态：背景 hub 也含学校名，断言限定对话框内。
  const wizard = page.getByRole('dialog')
  await expect(wizard.getByRole('heading', { name: '确认账号资料' })).toBeVisible()
  await expect(wizard.getByText('合成大学')).toBeVisible()
  await expect(wizard.getByText('合成学校甲')).toBeVisible()
  await expect(wizard.getByText('平台已有设备码；此处不展示完整内容')).toBeVisible()

  await page.getByRole('button', { name: '继续' }).click()
  await expect(page.getByRole('heading', { name: '添加现场照片' })).toBeVisible()

  await page.getByRole('button', { name: /跳过照片并提交/ }).click()
  await expect(page.getByRole('heading', { name: '已提交审核' })).toBeVisible()
})

test('history deep link shows the month and day detail', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/history?day=2026-11-10')

  await expect(page.locator('.uapp-cal-title strong')).toContainText('2026年11月')
  await expect(page.locator('.uapp-day-detail')).toContainText('11月10日')
  await expect(page.locator('.uapp-day-detail')).toContainText('东区宿舍')
  await expect(page.locator('.uapp-day-detail')).toContainText('成功')

  await page.getByRole('button', { name: /11月9日/ }).click()
  await expect(page).toHaveURL(/day=2026-11-09/)
})

test('settings shows preferences and toggles failure mail', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/settings')

  await expect(page.locator('.uapp-greeting')).toHaveText('设置')
  await expect(page.locator('.uapp-row-label', { hasText: '签到时间' })).toBeVisible()
  await expect(page.locator('.uapp-row-label', { hasText: '签到失败邮件' })).toBeVisible()

  const toggle = page.getByRole('switch', { name: '签到失败邮件' })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
})

test('time preference saves a slot and shows the absolute time', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/settings/time-pref')

  // 子表单弹窗化：深链落在设置页＋模态，标题钉对话框内。
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: '签到时间' })).toBeVisible()
  await dialog.getByRole('option', { name: /06:50/ }).click()
  await dialog.getByRole('button', { name: '保存' }).click()
  await expect(dialog.locator('.uapp-hero-time')).toHaveText('06:50')

  // 关闭后深链归位设置页，URL 与内容一致。
  await dialog.getByRole('button', { name: '关闭弹窗' }).click()
  await expect(page).toHaveURL(/\/app\/settings$/)
})

test('settings rows open time and password dialogs in place', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/settings')

  await page.getByRole('button', { name: /签到时间/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: '签到时间' })).toBeVisible()
  await dialog.getByRole('button', { name: '关闭弹窗' }).click()
  await expect(dialog).toHaveCount(0)

  await page.getByRole('button', { name: '修改密码' }).click()
  await expect(page.getByRole('dialog').getByRole('heading', { name: '修改密码' })).toBeVisible()
  await expect(page.locator('#uapp-pw-current')).toBeVisible()
})

test('failure mail switch round-trips without sticking', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/settings')

  const toggle = page.getByRole('switch', { name: '签到失败邮件' })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  // 连点不卡死：pending 期间置灰，落定后下一次点击才生效；往返终态与服务端一致。
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
  await page.goto('/app/settings/password')

  await page.locator('#uapp-pw-current').fill('not-a-real-password')
  await page.locator('#uapp-pw-next').fill('short')
  await page.locator('#uapp-pw-confirm').fill('short')
  await page.getByRole('button', { name: '保存新密码' }).click()
  await expect(page.getByText('新密码不符合规则')).toBeVisible()
})

test('delete login account sends the site password', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/settings')

  await page.locator('.uapp-row--danger', { hasText: '删除登录账号' }).click()
  await expect(page.getByRole('dialog')).toContainText('本站登录口令')
  await page.locator('#uapp-reauth-password').fill('not-a-real-password')

  const request = page.waitForRequest(
    (req) => req.url().endsWith('/api/me/delete') && req.method() === 'POST',
  )
  await page
    .getByRole('dialog')
    .getByRole('button', { name: '删除登录账号' })
    .click()
  const body = (await request).postDataJSON() as { current_password?: string }
  expect(body.current_password).toBe('not-a-real-password')
})

test('credential edit sends current_password and expected_updated_at', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/account')

  await page.locator('.uapp-row', { hasText: '易班资料' }).click()
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
  await page.goto('/app/account')

  await page.locator('.uapp-row', { hasText: '易班资料' }).click()
  await page.locator('#uapp-edit-name').fill('新备注')

  const request = page.waitForRequest(
    (req) =>
      req.url().endsWith(`/api/accounts/${USER_ACCOUNT.id}`) && req.method() === 'PATCH',
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
  await page.goto('/app/account')

  let patched = 0
  page.on('request', (req) => {
    if (req.url().endsWith(`/api/accounts/${USER_ACCOUNT.id}`) && req.method() === 'PATCH') {
      patched += 1
    }
  })

  await page.locator('.uapp-row', { hasText: '易班资料' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存' }).click()
  await expect(page.getByRole('dialog')).toContainText('没有需要保存的修改。')
  expect(patched).toBe(0)
})

test('account delete and refresh send the site password', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/account')

  await page.locator('.uapp-row--danger', { hasText: '删除账号' }).click()
  await page.locator('#uapp-reauth-password').fill('not-a-real-password')
  const deleteRequest = page.waitForRequest(
    (req) =>
      req.url().endsWith(`/api/me/accounts/${USER_ACCOUNT.id}/delete`) &&
      req.method() === 'POST',
  )
  await page.getByRole('dialog').getByRole('button', { name: '删除账号' }).click()
  const deleteBody = (await deleteRequest).postDataJSON() as {
    current_password?: string
  }
  expect(deleteBody.current_password).toBe('not-a-real-password')

  await page.goto('/app/account')
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

test('restore deleted signing account sends the site password', async ({ page }) => {
  await mockUserAppApi(page, {
    role: 'user',
    accounts: [{ ...USER_ACCOUNT, deleted_at: '2026-11-10T00:00:00Z' }],
    signStatus: [],
    diagnostics: [],
  })
  await page.goto('/app/account')

  await expect(page.locator('.uapp-status')).toContainText('已删除')
  await page.locator('.uapp-row--danger', { hasText: '恢复账号' }).click()
  await page.locator('#uapp-reauth-password').fill('not-a-real-password')
  const request = page.waitForRequest(
    (req) =>
      req.url().endsWith(`/api/me/accounts/${USER_ACCOUNT.id}/restore`) &&
      req.method() === 'POST',
  )
  await page.getByRole('dialog').getByRole('button', { name: '恢复账号' }).click()
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

  // 首登 401 → 自动 restore（同邮箱+口令）→ 重发登录 → 落到 /app。
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

  // restore 也失败：不泄漏「被删除」，仍是一句通用凭据错误；不发生第三次登录。
  await expect(page.getByRole('alert')).toContainText('邮箱或密码错误')
  await expect(page).toHaveURL(/\/login$/)
})

test('user-app UX invariants: touch targets >=44px, contrast >=4.5, unclipped streak, keyboard time-pref', async ({
  page,
}) => {
  await mockUserAppApi(page, { role: 'user', announcement: '维护通知' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/app')

  // Skip link exists for keyboard users and moves focus cleanly.
  // 按压时机要重试：React 挂载前的那次 Tab 会落在随后被卸载的节点上，焦点回落 body，
  // 而 toBeFocused 只会干等 5 秒、不会重按。重按仍证明「Tab 能到达跳转链接」。
  await expect(async () => {
    await page.keyboard.press('Tab')
    await expect(page.locator('.uapp-skip')).toBeFocused({ timeout: 1_000 })
  }).toPass({ timeout: 10_000 })

  // Announcement dismiss button meets >= 44x44px hit target.
  const closeBox = await page.locator('.uapp-announcement-close').boundingBox()
  expect(closeBox?.width).toBeGreaterThanOrEqual(44)
  expect(closeBox?.height).toBeGreaterThanOrEqual(44)

  // Streak value does not double-stack clipped glyphs via negative margin.
  const streakMargin = await page.evaluate(() => {
    const nf = document.querySelector('.uapp-stat-value .app-number')
    return nf ? parseFloat(getComputedStyle(nf).marginBlockStart) : 0
  })
  expect(streakMargin).toBeGreaterThanOrEqual(0)

  // Calendar grid at 390 keeps every day cell >= 44px in both axes + selection semantics.
  await page.goto('/app/history?day=2026-11-10')
  // 日历格由 /api/me/sign-ins 回读后渲染：先等首格出现再量宽，0 宽只说明还没画出来。
  await expect(page.locator('.uapp-cal-cell').first()).toBeVisible()
  const cellBox = await page.evaluate(() => {
    const cell = document.querySelector('.uapp-cal-cell')
    return cell ? cell.getBoundingClientRect() : null
  })
  expect(cellBox?.width).toBeGreaterThanOrEqual(44)
  expect(cellBox?.height).toBeGreaterThanOrEqual(44)
  await expect(page.locator('.uapp-cal-cell.is-selected')).toHaveAttribute('aria-pressed', 'true')

  // Time preference slots >= 44px wide and support ArrowRight keyboard selection.
  await page.goto('/app/settings/time-pref')
  // 同理：尺子档位由 /api/me/time-pref 回读后渲染，未出画时 width 恒为 0。
  await expect(page.locator('.uapp-ruler-tick').first()).toBeVisible()
  const tickWidth = await page.evaluate(() => {
    const tick = document.querySelector('.uapp-ruler-tick')
    return tick ? tick.getBoundingClientRect().width : 0
  })
  expect(tickWidth).toBeGreaterThanOrEqual(44)
  const selected = page.getByRole('dialog').locator('.uapp-ruler-tick[aria-selected="true"]')
  await selected.focus()
  await selected.press('ArrowRight')
  await expect(page.getByRole('dialog').locator('.uapp-hero-time')).not.toHaveText('06:45')
})

test('time preference skips disabled leading slot for keyboard tabIndex', async ({ page }) => {
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
  await page.goto('/app/settings/time-pref')

  const options = page.getByRole('dialog').locator('[role="option"]')
  await expect(options.nth(0)).toHaveAttribute('tabindex', '-1')
  await expect(options.nth(1)).toHaveAttribute('tabindex', '0')
})

test('password dialog clears must_change_password banner cleanly and handles session expiry', async ({
  page,
}) => {
  await mockUserAppApi(page, { role: 'user', mustChangePassword: true })
  await page.route('**/api/auth/change-password', (route) =>
    route.fulfill({ status: 204, body: '' }),
  )
  await page.goto('/app/settings/password')
  await expect(page.getByText('登录口令需要重置。')).toBeVisible()

  await page.locator('#uapp-pw-current').fill('OldPass123!')
  await page.locator('#uapp-pw-next').fill('NewStrong1!')
  await page.locator('#uapp-pw-confirm').fill('NewStrong1!')
  await page.getByRole('button', { name: '保存' }).click()

  await expect(page.getByRole('dialog')).toBeHidden()
  await expect(page.getByText('登录口令需要重置。')).toBeHidden()

  // SessionExpiredError during change-password triggers expireSession without bouncing back.
  await page.goto('/app/settings/password')
  await expect(page.locator('#uapp-pw-current')).toBeVisible()
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
  await page.locator('#uapp-pw-current').fill('OldPass123!')
  await page.locator('#uapp-pw-next').fill('NewStrong2!')
  await page.locator('#uapp-pw-confirm').fill('NewStrong2!')
  await page.getByRole('button', { name: '保存' }).click()

  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByText('会话已过期，请重新登录')).toBeVisible()
})

test('wizard step 1 manual school switch validates phone and password first', async ({ page }) => {
  await mockUserAppApi(page, { role: 'user' })
  await page.goto('/app/account/new')

  const wizard = page.getByRole('dialog')
  await wizard.getByRole('button', { name: '手动选择学校' }).click()
  await expect(wizard.getByRole('alert')).toContainText('11 位易班手机号')
  await expect(page.locator('#uapp-new-phone')).toBeFocused()
  await expect(wizard.getByRole('heading', { name: '连接你的易班' })).toBeVisible()
})
