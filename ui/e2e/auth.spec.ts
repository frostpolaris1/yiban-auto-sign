import { test, expect, type Page } from '@playwright/test'
import {
  loginCalls,
  logoutCalls,
  mockAccountsApi,
  SESSION_EMAIL,
  SESSION_PASSWORD,
} from './support/fake-accounts-api'

/**
 * 切片 3 的 8 条不变式：会话引导、路由守卫、登录、登出、过期处理、改密提示。
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
