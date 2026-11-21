import { test, expect, type Page } from '@playwright/test'
import {
  DEFAULT_NOTIFY_PREFS,
  SESSION_CSRF,
  SESSION_EMAIL,
  SESSION_PASSWORD,
  changePasswordCalls,
  mockAccountsApi,
  notifyGetCalls,
  notifySetCalls,
  type FakeApi,
} from './support/fake-accounts-api'

/**
 * 账号设置页与登录态改密的不变式。
 *
 * 模拟的真实用户动作：登录用户打开顶栏「账号设置」，看一眼自己的邮箱与角色，
 * 再填当前口令、新口令、确认新口令并提交。
 * 断言用户可见结果：toast 文案、提示条消失、字段清空、错误文案、请求体形状。
 * 401 的两种来源要分开：me 探针仍 200 ⇒ 当前口令错，留本页；
 * me 探针也 401 ⇒ 会话失效，落登录页并显示过期提示，不得提示口令错。
 *
 * 不钉组件名，不钉调用顺序。请求次数用假 API 的记录判。
 */

/** 合成新口令。明示不是真口令，与认证用例同族。 */
const NEW_PASSWORD = 'not-a-real-new-password'

const WRONG_CURRENT = 'wrong-password'

function toast(page: Page) {
  return page.locator('[data-sonner-toast]')
}

async function openSettings(page: Page): Promise<void> {
  await page.goto('/settings')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('账号设置')
  await expect(page.getByRole('heading', { name: '修改密码' })).toBeVisible()
}

async function submitChange(
  page: Page,
  current: string,
  next: string,
  confirmation = next,
): Promise<void> {
  await page.getByLabel(/^当前密码/).fill(current)
  await page.getByLabel(/^新密码/).fill(next)
  await page.getByLabel(/^确认新密码/).fill(confirmation)
  await page.getByRole('button', { name: '修改密码' }).click()
}

/** 改密请求之后发出的 me 探针。用于判 401 有没有被探针分诊。 */
function meProbesAfterChange(api: FakeApi) {
  const changeAt = api.calls.findIndex((call) => call.path === '/api/auth/change-password')
  return api.calls
    .slice(changeAt + 1)
    .filter((call) => call.method === 'GET' && call.path === '/api/auth/me')
}

test('settings: the account card shows email, role and status only', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })
  await openSettings(page)

  const card = page.locator('.card').filter({ hasText: '账号信息' })
  await expect(card).toContainText(SESSION_EMAIL)
  await expect(card).toContainText('超级管理员')
  await expect(card).toContainText('正常')
  // 会话接口没有上次改密时间：界面不得自行造一个字段。
  await expect(card.getByText(/上次改密/)).toHaveCount(0)
})

test('settings: a successful change clears the form, toasts, and drops the banner', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master', mustChangePassword: true })
  await openSettings(page)
  await expect(page.getByText('账号需要重置密码')).toBeVisible()

  await submitChange(page, SESSION_PASSWORD, NEW_PASSWORD)

  await expect(toast(page)).toContainText('密码已修改')
  await expect(page.getByText('账号需要重置密码')).toHaveCount(0)
  await expect(page.getByLabel(/^当前密码/)).toHaveValue('')
  await expect(page.getByLabel(/^新密码/)).toHaveValue('')
  await expect(page.getByLabel(/^确认新密码/)).toHaveValue('')
  await expect(page).toHaveURL(/\/settings$/)

  const calls = changePasswordCalls(api)
  expect(calls, 'exactly one change-password request').toHaveLength(1)
  expect(calls[0].body).toEqual({
    current_password: SESSION_PASSWORD,
    new_password: NEW_PASSWORD,
  })
  expect(Object.keys(calls[0].body as Record<string, unknown>).sort()).toEqual([
    'current_password',
    'new_password',
  ])
  expect(calls[0].headers['x-csrf-token'], 'the write carries the session CSRF token').toBe(
    SESSION_CSRF,
  )
})

test('settings: a wrong current password says so and keeps the session', async ({ page }) => {
  const api = await mockAccountsApi(page, {
    role: 'master',
    changePasswordError: { status: 401, code: 'unauthorized', message: 'invalid email or password' },
  })
  await openSettings(page)

  await submitChange(page, WRONG_CURRENT, NEW_PASSWORD)

  await expect(page.getByText('当前密码不正确')).toBeVisible()
  // 401 有两种来源。me 探针仍返回会话 ⇒ 是旧口令错：留在本页，不显示过期提示。
  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByText('会话已过期，请重新登录')).toHaveCount(0)
  // 后端信封原文不得直接展示：界面用固定文案。
  await expect(page.getByText('invalid email or password')).toHaveCount(0)
  await expect(page.getByLabel(/^当前密码/)).toHaveValue(WRONG_CURRENT)
  expect(changePasswordCalls(api), 'the request was attempted once').toHaveLength(1)
  expect(meProbesAfterChange(api), 'the 401 was disambiguated by one me probe').toHaveLength(1)
})

test('settings: a 401 from a dead session lands on login, not a password error', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master', changePasswordSessionExpired: true })
  await openSettings(page)

  await submitChange(page, SESSION_PASSWORD, NEW_PASSWORD)

  // 会话没了：落登录页并说明过期，不能把用户引去猜当前口令。
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByText('会话已过期，请重新登录')).toBeVisible()
  await expect(page.getByText('当前密码不正确')).toHaveCount(0)
  expect(changePasswordCalls(api), 'the change was attempted once').toHaveLength(1)
  // 探针一次；清缓存后守卫与登录页还会各自再取会话，故只判「至少一次」。
  expect(meProbesAfterChange(api).length, 'the page probed me after the 401').toBeGreaterThanOrEqual(
    1,
  )
})

test('settings: a reused password reports the server conflict in product words', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, {
    role: 'master',
    changePasswordError: {
      status: 409,
      code: 'conflict',
      message: 'new password must differ from the current password',
    },
  })
  await openSettings(page)

  // 本地输入不同，服务端仍判冲突：界面必须照服务端结论说话。
  await submitChange(page, SESSION_PASSWORD, `${SESSION_PASSWORD}-other`)

  await expect(page.getByText('新密码不能与当前密码相同')).toBeVisible()
  await expect(page.getByText('new password must differ')).toHaveCount(0)
  expect(changePasswordCalls(api), 'the request was attempted once').toHaveLength(1)
})

test('settings: a validation envelope is shown as the envelope', async ({ page }) => {
  const api = await mockAccountsApi(page, {
    role: 'master',
    changePasswordError: {
      status: 400,
      code: 'validation_failed',
      message: 'new_password is required',
    },
  })
  await openSettings(page)

  await submitChange(page, SESSION_PASSWORD, NEW_PASSWORD)

  const alert = page.getByRole('alert').filter({ hasText: 'validation_failed' })
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('new_password is required')
  expect(changePasswordCalls(api), 'the request was attempted once').toHaveLength(1)
})

test('settings: a network failure is reported and the form stays', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'master', changePasswordNetworkError: true })
  await openSettings(page)

  await submitChange(page, SESSION_PASSWORD, NEW_PASSWORD)

  const alert = page.getByRole('alert').filter({ hasText: '网络异常，请稍后重试' })
  await expect(alert).toBeVisible()
  await expect(page.getByLabel(/^当前密码/)).toHaveValue(SESSION_PASSWORD)
  expect(changePasswordCalls(api), 'the request was attempted once').toHaveLength(1)
})

test('settings: a mismatched confirmation is stopped in the page', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openSettings(page)

  await submitChange(page, SESSION_PASSWORD, NEW_PASSWORD, `${NEW_PASSWORD}-other`)

  await expect(page.getByText('两次输入的新密码不一致')).toBeVisible()
  await expect(page.getByLabel(/^确认新密码/)).toBeFocused()
  expect(changePasswordCalls(api), 'a local mismatch sends nothing').toHaveLength(0)
})

test('settings: a new password equal to the current one is stopped in the page', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openSettings(page)

  await submitChange(page, SESSION_PASSWORD, SESSION_PASSWORD)

  await expect(page.getByText('新密码不能与当前密码相同')).toBeVisible()
  await expect(page.getByLabel(/^新密码/)).toBeFocused()
  expect(changePasswordCalls(api), 'a reused password sends nothing').toHaveLength(0)
})

test('settings: every empty field is reported before any request', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openSettings(page)

  await page.getByRole('button', { name: '修改密码' }).click()

  await expect(page.getByText('请输入当前密码')).toBeVisible()
  await expect(page.getByText('请输入新密码')).toBeVisible()
  await expect(page.getByText('请再次输入新密码')).toBeVisible()
  await expect(page.getByLabel(/^当前密码/)).toBeFocused()
  expect(changePasswordCalls(api), 'empty fields send nothing').toHaveLength(0)
})

test('settings: an unauthenticated visitor lands on login and returns after sign-in', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: null })
  await page.goto('/settings')

  await expect(page).toHaveURL(/\/login\?redirect=%2Fsettings$/)
  await page.getByLabel('邮箱').fill(SESSION_EMAIL)
  await page.locator('#login-password').fill(SESSION_PASSWORD)
  await page.getByRole('button', { name: '登录', exact: true }).click()

  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByRole('heading', { name: '修改密码' })).toBeVisible()
  expect(changePasswordCalls(api), 'no password change before sign-in').toHaveLength(0)
})

test('settings: the keyboard alone completes the change', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openSettings(page)

  await page.getByLabel(/^当前密码/).pressSequentially(SESSION_PASSWORD)
  await page.getByLabel(/^当前密码/).press('Tab')
  await page.getByLabel(/^新密码/).pressSequentially(NEW_PASSWORD)
  await page.getByLabel(/^新密码/).press('Tab')
  await page.getByLabel(/^确认新密码/).pressSequentially(NEW_PASSWORD)
  await page.getByLabel(/^确认新密码/).press('Enter')

  await expect(toast(page)).toContainText('密码已修改')
  expect(changePasswordCalls(api), 'Enter submits the form').toHaveLength(1)
})

test('settings: the reset banner link opens the settings page', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master', mustChangePassword: true })
  await page.goto('/')

  await expect(page.getByText('账号需要重置密码')).toBeVisible()
  await page.getByRole('link', { name: '去修改' }).click()

  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByRole('heading', { name: '修改密码' })).toBeVisible()
})

test('settings: the profile menu has one account entry and the sidebar lists it', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: 'master' })
  await page.goto('/')

  const sidebar = page.locator('.d-sidebar')
  await expect(sidebar.getByRole('link', { name: '设置', exact: true })).toBeVisible()

  await page.getByRole('button', { name: '账号菜单' }).click()
  const menu = page.getByRole('menu')
  await expect(menu.getByRole('menuitem', { name: '个人信息' })).toHaveCount(0)
  await menu.getByRole('menuitem', { name: '账号设置' }).click()
  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByRole('heading', { name: '修改密码' })).toBeVisible()
})

const NOTIFY_CATEGORY_LABELS: Record<string, string> = {
  credential_invalid: '凭据失效',
  review_pending: '账号待审',
  sign_failed_daily: '每日失败摘要',
  capacity_rejected: '容量驳回',
}

const NOTIFY_CHANNEL_LABELS: Record<string, string> = {
  in_app: '站内信',
  email: '邮件',
  webhook: 'Webhook',
}

for (const role of ['master', 'admin'] as const) {
  test(`settings: ${role} renders the 4x3 notification matrix from GET`, async ({ page }) => {
    const api = await mockAccountsApi(page, { role })
    await openSettings(page)

    const card = page.locator('.card').filter({ hasText: '通知订阅' })
    await expect(card).toBeVisible()
    for (const label of Object.values(NOTIFY_CATEGORY_LABELS)) {
      await expect(card.getByText(label, { exact: true })).toBeVisible()
    }
    for (const label of Object.values(NOTIFY_CHANNEL_LABELS)) {
      await expect(card.getByRole('columnheader', { name: label, exact: true })).toBeVisible()
    }
    for (const item of DEFAULT_NOTIFY_PREFS) {
      const name = `${NOTIFY_CATEGORY_LABELS[item.category]} · ${NOTIFY_CHANNEL_LABELS[item.channel]}`
      await expect(card.getByRole('switch', { name })).toHaveAttribute(
        'aria-checked',
        String(item.enabled),
      )
    }
    expect(notifyGetCalls(api), 'one initial GET for notification prefs').toHaveLength(1)
  })
}

test('settings: toggling one notification switch sends one full PUT, toasts, and refetches', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openSettings(page)

  const toggle = page.getByRole('switch', { name: '凭据失效 · 邮件' })
  await expect(toggle).toHaveAttribute('aria-checked', 'false')

  await toggle.click()

  await expect(toast(page)).toContainText('通知订阅已保存')
  await expect(toggle).toHaveAttribute('aria-checked', 'true')

  const puts = notifySetCalls(api)
  expect(puts, 'exactly one PUT per toggle').toHaveLength(1)
  expect(puts[0].headers['x-csrf-token']).toBe(SESSION_CSRF)
  const expectedItems = DEFAULT_NOTIFY_PREFS.map((item) =>
    item.category === 'credential_invalid' && item.channel === 'email'
      ? { ...item, enabled: true }
      : item,
  )
  expect(puts[0].body).toEqual({ items: expectedItems })
  await expect.poll(() => notifyGetCalls(api).length).toBeGreaterThanOrEqual(2)
})

test('settings: keyboard Space toggles a notification switch, suppresses duplicate Space, and keeps focus', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'admin', holdNotifyPut: true })
  await openSettings(page)

  const toggle = page.getByRole('switch', { name: '每日失败摘要 · 站内信' })
  const peerToggle = page.getByRole('switch', { name: '凭据失效 · 站内信' })
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await toggle.focus()
  await toggle.press('Space')

  // 在途期间保持焦点并锁定全表，连续按空格不得重发 PUT。
  await expect(toggle).toHaveAttribute('aria-busy', 'true')
  await expect(toggle).toHaveAttribute('aria-disabled', 'true')
  await expect(peerToggle).toHaveAttribute('aria-disabled', 'true')
  await expect(page.getByText('保存中…')).toBeVisible()
  await expect(toggle).toBeFocused()
  await toggle.press('Space')
  expect(notifySetCalls(api)).toHaveLength(1)

  api.releaseNotifyPut()

  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await expect(toggle).not.toHaveAttribute('aria-busy', 'true')
  await expect(toggle).toBeFocused()
  expect(notifySetCalls(api)).toHaveLength(1)
})

test('settings: clicking another switch while a save is in-flight is ignored', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master', holdNotifyPut: true })
  await openSettings(page)

  const toggle = page.getByRole('switch', { name: '每日失败摘要 · 站内信' })
  const peerToggle = page.getByRole('switch', { name: '凭据失效 · 站内信' })
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await expect(peerToggle).toHaveAttribute('aria-checked', 'true')

  await toggle.click()
  await expect.poll(() => notifySetCalls(api).length).toBe(1)
  await expect(peerToggle).toHaveAttribute('aria-disabled', 'true')

  await peerToggle.click({ force: true })
  expect(notifySetCalls(api)).toHaveLength(1)

  api.releaseNotifyPut()

  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await expect(peerToggle).toHaveAttribute('aria-checked', 'true')
  expect(notifySetCalls(api)).toHaveLength(1)
})

test('settings: a failed notification save keeps the switch in place and reports inline', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, {
    role: 'master',
    notifySetError: {
      status: 400,
      code: 'validation_failed',
      message: 'invalid notification subscription',
    },
  })
  await openSettings(page)

  const toggle = page.getByRole('switch', { name: '凭据失效 · 邮件' })
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await toggle.click()

  await expect
    .poll(() => notifySetCalls(api).length)
    .toBe(1)
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  const row = page.locator('tr').filter({ hasText: '凭据失效' })
  await expect(row.getByRole('alert')).toContainText(
    'validation_failed · invalid notification subscription',
  )
  await expect(toast(page)).toHaveCount(0)
})

test('settings: a notification network failure keeps the switch in place and reports inline', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master', notifyNetworkError: true })
  await openSettings(page)

  const toggle = page.getByRole('switch', { name: '容量驳回 · Webhook' })
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await toggle.click()

  await expect.poll(() => notifySetCalls(api).length).toBe(1)
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  const row = page.locator('tr').filter({ hasText: '容量驳回' })
  await expect(row.getByRole('alert')).toContainText('网络异常，请稍后重试')
  await expect(toast(page)).toHaveCount(0)
})

test('settings: initial notification load error offers retry and recovers', async ({ page }) => {
  await mockAccountsApi(page, {
    role: 'master',
    notifyGetError: { status: 500, code: 'internal_error', message: 'db down', once: true },
  })
  await openSettings(page)

  const card = page.locator('.card').filter({ hasText: '通知订阅' })
  await expect(card.getByRole('alert')).toContainText('db down')
  await card.getByRole('button', { name: '重试' }).click()

  await expect(card.getByRole('switch', { name: '凭据失效 · 站内信' })).toHaveAttribute(
    'aria-checked',
    'true',
  )
})

test('settings: user role lands on the denied card with zero business requests', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'user' })
  await page.goto('/settings')

  await expect(page.getByRole('heading', { name: '无权访问' })).toBeVisible()
  await expect(page.getByText('你的角色无权访问账号设置')).toBeVisible()
  await expect(page.getByRole('button', { name: '返回看板' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '修改密码' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '通知订阅' })).toHaveCount(0)

  const businessCalls = api.calls.filter((call) => call.path !== '/api/auth/me')
  expect(businessCalls, 'user role sends zero business requests on /settings').toEqual([])
})

test('settings: 390px viewport renders the notification matrix without horizontal overflow', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockAccountsApi(page, { role: 'master' })
  await openSettings(page)

  await expect(page.getByRole('switch', { name: '容量驳回 · Webhook' })).toBeVisible()
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  expect(
    overflow.scrollWidth,
    `390px overflow: ${overflow.scrollWidth} > ${overflow.clientWidth}`,
  ).toBeLessThanOrEqual(overflow.clientWidth + 1)
})
