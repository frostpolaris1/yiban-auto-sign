import { test, expect, type Page } from '@playwright/test'
import {
  forgotPasswordCalls,
  loginCalls,
  logoutCalls,
  mockAccountsApi,
  resetPasswordCalls,
  RESET_TOKEN,
  SESSION_EMAIL,
  SESSION_PASSWORD,
  type FakeApi,
} from './support/fake-accounts-api'

/**
 * 切片 3 的 8 条不变式：会话引导、路由守卫、登录、登出、过期处理、改密提示。
 * 密码找回两页的不变式接在其后。
 *
 * 每条断言用户可见结果：落点 URL、屏幕上的文案、请求体的形状。
 * 不钉组件名、不钉调用顺序。
 */

async function submitLogin(page: Page, email = SESSION_EMAIL, password = SESSION_PASSWORD) {
  await page.getByLabel('邮箱').fill(email)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录控制台' }).click()
}

test('invariant 1: an unauthenticated shell route lands on login and returns after sign-in', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/accounts')

  await expect(page).toHaveURL(/\/login\?redirect=%2Faccounts$/)
  await expect(page.getByRole('heading', { name: '登录', exact: true })).toBeVisible()

  await submitLogin(page)
  await expect(page).toHaveURL(/\/accounts$/)
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()
})

for (const offSite of ['%2F%2Fevil.com', 'https%3A%2F%2Fevil.com', '%2F.%2F%2Fevil.com']) {
  test(`invariant 1: the off-site redirect ${offSite} is dropped`, async ({ page }) => {
    await mockAccountsApi(page, { role: null })
    await page.goto(`/login?redirect=${offSite}`)

    await submitLogin(page)
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('heading', { name: '登录', exact: true })).toHaveCount(0)
  })
}

for (const variant of [
  '%2Flogin',
  '%2Flogin%2F',
  '%2FLOGIN',
  '%2Flogin%2F%2F',
  '%2Flogin%2F.',
  // 路由器匹配前做 URI 解码：这些编码拼写同样命中登录页。
  '%2F%256Cogin',
  '%2Flog%2569n',
  '%2F%256C%256F%2567%2569%256E',
]) {
  test(`invariant 1: the login redirect ${variant} never lands back on the login page`, async ({
    page,
  }) => {
    await mockAccountsApi(page, { role: null })
    await page.goto(`/login?redirect=${variant}`)

    await submitLogin(page)
    // 任何拼写都不得把已登录用户留在表单上：漏网时登录页守卫接手。
    await expect(page.getByRole('heading', { name: '登录', exact: true })).toHaveCount(0)
    await expect(page).toHaveURL('/')
  })
}

test('invariant 2: a signed-in visitor is sent away from the login page', async ({ page }) => {
  await mockAccountsApi(page, { role: 'admin' })
  await page.goto('/login')

  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('heading', { name: '登录', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '账号菜单' })).toBeVisible()
})

test('invariant 3: the login request carries only email and password', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  await submitLogin(page)
  await expect(page).toHaveURL(/\/$/)

  const calls = loginCalls(api)
  expect(calls, 'exactly one login attempt').toHaveLength(1)
  expect(calls[0].body).toEqual({ email: SESSION_EMAIL, password: SESSION_PASSWORD })
  expect(Object.keys(calls[0].body as Record<string, unknown>).sort()).toEqual([
    'email',
    'password',
  ])
})

test('invariant 3: a rejected credential shows the message and keeps the URL', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  await submitLogin(page, SESSION_EMAIL, 'wrong-password')
  await expect(page.getByRole('alert')).toContainText('邮箱或密码错误')
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByRole('heading', { name: '登录', exact: true })).toBeVisible()
})

test('invariant 4: no long-lived session control exists on the login page', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  await expect(page.getByRole('heading', { name: '登录', exact: true })).toBeVisible()
  await expect(page.locator('.auth-form input[type="checkbox"]')).toHaveCount(0)
  await expect(page.getByText(/保持登录|记住我/)).toHaveCount(0)
})

test('invariant 5: logout posts with the CSRF header and ends the session', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await page.goto('/accounts')
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()

  await page.getByRole('button', { name: '账号菜单' }).click()
  await page.getByRole('menuitem', { name: '退出登录' }).click()
  await expect(page).toHaveURL(/\/login$/)

  const calls = logoutCalls(api)
  expect(calls, 'exactly one logout request').toHaveLength(1)
  expect(calls[0].headers['x-csrf-token'], 'logout carries the session CSRF token').toBe(
    'csrf-test',
  )

  await page.goto('/accounts')
  await expect(page).toHaveURL(/\/login\?redirect=%2Faccounts$/)
})

test('invariant 6: a business 401 expires the session and says so', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master', expireOn: ['/api/accounts'] })
  await page.goto('/accounts')

  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByText('会话已过期，请重新登录')).toBeVisible()
})

test('invariant 6: a login 401 is a credential error, not an expiry', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  await submitLogin(page, SESSION_EMAIL, 'wrong-password')
  await expect(page.getByRole('alert')).toContainText('邮箱或密码错误')
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByText('会话已过期，请重新登录')).toHaveCount(0)
})

test('invariant 6: the expiry notice clears after a successful sign-in', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master', expireOn: ['/api/accounts'] })
  await page.goto('/accounts')
  await expect(page.getByText('会话已过期，请重新登录')).toBeVisible()

  await submitLogin(page)
  await expect(page).toHaveURL(/\/$/)

  // 再登出回到登录页：上一次的过期提示不该回潮。
  await page.getByRole('button', { name: '账号菜单' }).click()
  await page.getByRole('menuitem', { name: '退出登录' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByText('会话已过期，请重新登录')).toHaveCount(0)
})

for (const required of [true, false]) {
  test(`invariant 7: must_change_password=${required} ${
    required ? 'shows' : 'hides'
  } the reset banner`, async ({ page }) => {
    await mockAccountsApi(page, { role: 'master', mustChangePassword: required })
    await page.goto('/')

    const banner = page.getByText('账号需要重置密码，请联系管理员')
    if (required) {
      await expect(banner).toBeVisible()
    } else {
      await expect(banner).toHaveCount(0)
    }
  })
}

test('invariant 8: the guard shows a loading state and never flashes the login page', async ({
  page,
}) => {
  await page.addInitScript(() => {
    const probe = { loginFrames: 0 }
    ;(window as unknown as { __loginProbe?: typeof probe }).__loginProbe = probe
    const scan = () => {
      if (document.querySelector('.auth-card')) probe.loginFrames += 1
    }
    new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true })
  })
  await mockAccountsApi(page, { role: 'master', meDelayMs: 700 })

  await page.goto('/accounts')
  await expect(page.locator('[data-session-loading]')).toBeVisible()

  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()
  await expect(page).toHaveURL(/\/accounts$/)

  const frames = await page.evaluate(
    () => (window as unknown as { __loginProbe: { loginFrames: number } }).__loginProbe.loginFrames,
  )
  expect(frames, 'the login page must not appear while the session is resolving').toBe(0)
})

// ---------- 密码找回：忘记密码与重置密码 ----------
//
// 两页都是公开页：未登录可用，已登录送回 `/`。
// 忘记密码恒 204，界面文案不得泄露账号是否存在。
// 重置密码的令牌从查询串读；令牌形状不合法时不发请求。

/** 合成新口令。明示不是真口令，形状与登录夹具同族。 */
const NEW_PASSWORD = 'not-a-real-new-password'

/** 公开登出型端点：任何一页都不该在用户没提交时打过去。 */
function expectNoPasswordPosts(api: FakeApi, label: string): void {
  expect(forgotPasswordCalls(api), `${label}: no forgot-password request`).toHaveLength(0)
  expect(resetPasswordCalls(api), `${label}: no reset-password request`).toHaveLength(0)
}

/** 断言焦点落在提示文案的容器上（提示本身或包住它的焦点落点）。 */
async function expectFocusOnNotice(page: Page, text: string): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(() => (document.activeElement?.textContent ?? '').trim()),
    )
    .toContain(text)
}

async function submitForgot(page: Page, email: string): Promise<void> {
  await page.getByLabel('邮箱').fill(email)
  await page.getByRole('button', { name: '发送重置链接' }).click()
}

async function submitReset(page: Page, password: string, confirmation: string): Promise<void> {
  await page.getByLabel('新密码').fill(password)
  await page.getByLabel('确认密码').fill(confirmation)
  await page.getByRole('button', { name: '重置密码' }).click()
}

test('forgot password: a registered address gets the fixed notice and no account hint', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: null })
  await page.goto('/forgot-password')

  await submitForgot(page, SESSION_EMAIL)

  await expect(page.getByText('如果该邮箱已注册，我们已发送重置链接。')).toBeVisible()
  // 不泄露账号是否存在：任何「不存在 / 未注册」类文案都不得出现。
  await expect(page.getByText(/不存在|未注册|没有该账号/)).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '请检查邮箱' })).toBeFocused()
  await expect(page.getByRole('link', { name: '返回登录' })).toHaveAttribute('href', '/login')

  const calls = forgotPasswordCalls(api)
  expect(calls, 'exactly one forgot-password request').toHaveLength(1)
  expect(calls[0].body).toEqual({ email: SESSION_EMAIL })
  expect(Object.keys(calls[0].body as Record<string, unknown>).sort()).toEqual(['email'])
})

test('forgot password: a rejected address keeps the form and marks the email field', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, {
    role: null,
    forgotError: { status: 400, code: 'validation_error', message: 'invalid email' },
  })
  await page.goto('/forgot-password')

  await submitForgot(page, SESSION_EMAIL)

  await expect(page.getByRole('alert')).toContainText('validation_error')
  await expect(page.getByLabel('邮箱')).toBeFocused()
  await expect(page.getByText('如果该邮箱已注册，我们已发送重置链接。')).toHaveCount(0)
  expect(forgotPasswordCalls(api), 'the request was attempted once').toHaveLength(1)
})

test('forgot password: a network failure is reported without leaving the form', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: null, forgotNetworkError: true })
  await page.goto('/forgot-password')

  await submitForgot(page, SESSION_EMAIL)

  await expect(page.getByRole('alert')).toContainText('网络异常，请稍后重试')
  await expectFocusOnNotice(page, '网络异常，请稍后重试')
  await expect(page.getByRole('button', { name: '发送重置链接' })).toBeVisible()
  await expect(page.getByText('如果该邮箱已注册，我们已发送重置链接。')).toHaveCount(0)
})

// 令牌形状取自服务端唯一生成点：32 字节经 base64url 无填充编码＝43 个字符。
const BAD_TOKENS = [
  { label: 'missing token', search: '' },
  { label: 'empty token', search: '?token=' },
  { label: 'short token', search: `?token=${'A'.repeat(42)}` },
  { label: 'non-base64url token', search: `?token=${'!'.repeat(43)}` },
] as const

for (const bad of BAD_TOKENS) {
  test(`reset password: the ${bad.label} link is rejected before any request`, async ({ page }) => {
    const api = await mockAccountsApi(page, { role: null })
    await page.goto(`/reset-password${bad.search}`)

    await expect(page.getByRole('heading', { name: '链接不完整或已失效' })).toBeVisible()
    await expect(page.getByRole('link', { name: '重新申请' })).toHaveAttribute(
      'href',
      '/forgot-password',
    )
    // 表单不得出现：用户没有可提交的内容。
    await expect(page.getByLabel('新密码')).toHaveCount(0)
    expectNoPasswordPosts(api, bad.label)
  })
}

test('reset password: mismatched entries are stopped in the page', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: null })
  await page.goto(`/reset-password?token=${RESET_TOKEN}`)

  await submitReset(page, NEW_PASSWORD, `${NEW_PASSWORD}-other`)

  await expect(page.getByText('两次输入的密码不一致')).toBeVisible()
  await expect(page.getByLabel('确认密码')).toBeFocused()
  await expect(page.getByLabel('新密码')).toBeVisible()
  expectNoPasswordPosts(api, 'mismatch')
})

test('reset password: a successful reset shows the done state and the login link', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: null })
  await page.goto(`/reset-password?token=${RESET_TOKEN}`)

  await submitReset(page, NEW_PASSWORD, NEW_PASSWORD)

  await expect(page.getByRole('heading', { name: '密码已重置' })).toBeFocused()
  await expect(page.getByRole('link', { name: '去登录' })).toHaveAttribute('href', '/login')

  const calls = resetPasswordCalls(api)
  expect(calls, 'exactly one reset request').toHaveLength(1)
  expect(calls[0].body).toEqual({ token: RESET_TOKEN, new_password: NEW_PASSWORD })
  expect(Object.keys(calls[0].body as Record<string, unknown>).sort()).toEqual([
    'new_password',
    'token',
  ])
})

test('reset password: a rejected body keeps the form and reports the envelope', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, {
    role: null,
    resetError: { status: 400, code: 'validation_error', message: 'new_password is required' },
  })
  await page.goto(`/reset-password?token=${RESET_TOKEN}`)

  await submitReset(page, NEW_PASSWORD, NEW_PASSWORD)

  await expect(page.getByRole('alert')).toContainText('validation_error')
  await expectFocusOnNotice(page, 'validation_error')
  await expect(page.getByLabel('新密码')).toBeVisible()
  await expect(page.getByRole('heading', { name: '密码已重置' })).toHaveCount(0)
  expect(resetPasswordCalls(api), 'the request was attempted once').toHaveLength(1)
})

test('reset password: an expired link is reported and does not expire the session', async ({
  page,
}) => {
  await mockAccountsApi(page, {
    role: null,
    resetError: { status: 401, code: 'unauthorized', message: '令牌无效或已过期' },
  })
  await page.goto(`/reset-password?token=${RESET_TOKEN}`)

  await submitReset(page, NEW_PASSWORD, NEW_PASSWORD)

  await expect(page.getByRole('heading', { name: '链接已过期或已使用' })).toBeFocused()
  await expect(page.getByRole('link', { name: '重新申请' })).toHaveAttribute(
    'href',
    '/forgot-password',
  )
  // 公开端点的 401 不是会话过期：站内走到登录页，不得出现过期提示。
  await page.getByRole('link', { name: '重新申请' }).click()
  await expect(page).toHaveURL(/\/forgot-password$/)
  await page.getByRole('link', { name: '返回登录' }).click()
  await expect(page.getByRole('heading', { name: '登录', exact: true })).toBeVisible()
  await expect(page.getByText('会话已过期，请重新登录')).toHaveCount(0)
})

test('password pages: a signed-in visitor is sent away from both', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })

  await page.goto('/forgot-password')
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('heading', { name: '忘记密码' })).toHaveCount(0)

  await page.goto(`/reset-password?token=${RESET_TOKEN}`)
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('heading', { name: '重置密码' })).toHaveCount(0)
})

test('reset password: the keyboard alone completes the flow', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: null })
  await page.goto(`/reset-password?token=${RESET_TOKEN}`)

  await page.getByLabel('新密码').pressSequentially(NEW_PASSWORD)
  await page.getByLabel('新密码').press('Tab')
  await page.getByLabel('确认密码').pressSequentially(NEW_PASSWORD)
  await page.getByLabel('确认密码').press('Enter')

  await expect(page.getByRole('heading', { name: '密码已重置' })).toBeVisible()
  expect(resetPasswordCalls(api), 'Enter submits the form').toHaveLength(1)
})
