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
  // 登录/注册双签常驻同页，「邮箱」「密码」标签各命中两签；一律用契约 id 定位。
  await page.locator('#login-email').fill(email)
  await page.locator('#login-password').fill(password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
}

test('invariant 1: an unauthenticated shell route lands on login and returns after sign-in', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/accounts')

  await expect(page).toHaveURL(/\/login\?redirect=%2Faccounts$/)
  await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible()

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
    await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toHaveCount(0)
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
    await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toHaveCount(0)
    await expect(page).toHaveURL('/')
  })
}

test('invariant 2: a signed-in visitor is sent away from the login page', async ({ page }) => {
  await mockAccountsApi(page, { role: 'admin' })
  await page.goto('/login')

  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toHaveCount(0)
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
  await expect(page.locator('.app-alert-block')).toBeFocused()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible()
})

test('invariant 4: no long-lived session control exists on the login page', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible()
  // 双签常驻：只查登录签（含 #login-email 的那一签），注册签的协议勾选不算会话控件。
  const loginPane = page.locator('.auth-mode-pane').filter({ has: page.locator('#login-email') })
  await expect(loginPane.locator('input[type="checkbox"]')).toHaveCount(0)
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
  await expect(page.locator('.app-alert-block')).toBeFocused()
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

    const banner = page.getByText('账号需要重置密码')
    if (required) {
      await expect(banner).toBeVisible()
      // 提示条必须可行动：出口指向可自助改密的设置页。
      await expect(page.getByRole('link', { name: '去修改' })).toHaveAttribute(
        'href',
        '/settings',
      )
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

test('invariant 9: the login form links to the forgot-password page', async ({ page }) => {
  await page.goto('/login')
  const link = page.getByRole('link', { name: '忘记密码？' })
  await expect(link).toHaveAttribute('href', '/forgot-password')
  await link.click()
  await expect(page).toHaveURL(/\/forgot-password$/)
  await expect(page.getByRole('heading', { name: '忘记密码' })).toBeVisible()
})

test('login: the password can be revealed without leaving the form', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  const password = page.locator('#login-password')
  await expect(password).toHaveAttribute('type', 'password')
  await page.getByRole('button', { name: '显示密码' }).click()
  await expect(password).toHaveAttribute('type', 'text')
  await expect(page.getByRole('button', { name: '隐藏密码' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await page.getByRole('button', { name: '隐藏密码' }).click()
  await expect(password).toHaveAttribute('type', 'password')
})

test('login: the public page keeps the brand and the theme toggle on mobile', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/login')

  await expect(page.locator('.auth-mobile-brand')).toContainText('易班自动签到')
  const html = page.locator('html')
  const initial = await html.getAttribute('data-theme')
  expect(initial).toMatch(/^(light|dark)$/)
  await page.getByRole('button', { name: /切换到/ }).click()
  await expect(html).not.toHaveAttribute('data-theme', initial ?? '')
})

test('login: portrait turns the sky into a page-wide backdrop and centers the panel', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: null })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/login')

  // 天空画布铺满视口当背景板；左栏文字层收起，避免与表单抢视线。
  const aside = page.locator('.auth-aside')
  await expect(aside).toBeVisible()
  await expect(aside.locator('.auth-brand')).toBeHidden()
  await expect(aside.locator('.auth-aside-body')).toBeHidden()
  await expect(aside.locator('.auth-aside-footer')).toBeHidden()
  const asideBox = await aside.boundingBox()
  const skyBox = await page.locator('.auth-sky').boundingBox()
  expect(asideBox).not.toBeNull()
  expect(asideBox!.width).toBeCloseTo(390, 0)
  expect(asideBox!.height).toBeCloseTo(844, 0)
  expect(skyBox).not.toBeNull()
  expect(skyBox!.width).toBeCloseTo(390, 0)
  expect(skyBox!.height).toBeCloseTo(844, 0)

  // 表单卡整块落在首屏内、上下留白大致相等，页脚仍在卡片下方。
  const card = await page.locator('.auth-card').boundingBox()
  expect(card).not.toBeNull()
  expect(card!.y).toBeGreaterThanOrEqual(0)
  expect(card!.y + card!.height).toBeLessThanOrEqual(844)
  expect(Math.abs(card!.y + card!.height / 2 - 844 / 2)).toBeLessThan(48)
  const footer = await page.locator('.auth-main-bottom').boundingBox()
  expect(footer).not.toBeNull()
  expect(footer!.y).toBeGreaterThan(card!.y + card!.height)
})

test('register: a short viewport scrolls to the submit button instead of clipping it', async ({
  page,
}) => {
  // 矮屏（390×520）放不下注册表单：卡片顺排、整页可滚，提交按钮初始被折线切到但滚得到。
  // 520 是实测挑的高度——640 时按钮完整落在折内，测不出「滚动」这件事。
  await mockAccountsApi(page, { role: null })
  await page.setViewportSize({ width: 390, height: 520 })
  await page.goto('/register')
  await expect(page.getByRole('heading', { name: '创建账号' })).toBeVisible()

  const metrics = await page.evaluate(() => ({
    scrollH: document.documentElement.scrollHeight,
    viewH: window.innerHeight,
    overflowX: document.documentElement.scrollWidth - window.innerWidth,
  }))
  expect(metrics.scrollH).toBeGreaterThan(metrics.viewH)
  expect(metrics.overflowX).toBeLessThanOrEqual(0)

  const submit = page.getByRole('button', { name: '创建账号' })
  // 断言「能滚到完整可见」而不是「量到某个 y」：表单高度随内容浮动，钉坐标会误报。
  const before = await submit.boundingBox()
  expect(before).not.toBeNull()
  expect(before!.y + before!.height).toBeGreaterThan(metrics.viewH)
  await submit.scrollIntoViewIfNeeded()
  const after = await submit.boundingBox()
  expect(after!.y).toBeGreaterThanOrEqual(0)
  expect(after!.y + after!.height).toBeLessThanOrEqual(metrics.viewH)
  // 走的是窗口滚动，不是某个内部滚动容器把按钮转进来。
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0)
})

test('auth: the portrait backdrop hands over to the desktop split at 901', async ({ page }) => {
  // 竖屏背景板与桌面分栏在 900/901 交接：900 仍铺满整块视口、露出窄屏品牌；
  // 901 回到左栏一列，桌面品牌接管。遍历式截图抓不到这条缝，所以钉在这里。
  await mockAccountsApi(page, { role: null })
  await page.setViewportSize({ width: 900, height: 800 })
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible()

  const portrait = await page.locator('.auth-aside').boundingBox()
  expect(portrait).not.toBeNull()
  expect(portrait!.width).toBeCloseTo(900, 0)
  expect(portrait!.height).toBeCloseTo(800, 0)
  const portraitSky = await page.locator('.auth-sky').boundingBox()
  expect(portraitSky).not.toBeNull()
  expect(portraitSky!.width).toBeCloseTo(900, 0)
  await expect(page.locator('.auth-mobile-brand')).toBeVisible()

  await page.setViewportSize({ width: 901, height: 800 })
  await expect(page.locator('.auth-mobile-brand')).toBeHidden()
  await expect(page.locator('.auth-aside .auth-brand')).toBeVisible()
  const split = await page.locator('.auth-aside').boundingBox()
  expect(split).not.toBeNull()
  // 桌面分栏：左栏占屏约三成（实测 436/901=0.48），天空退进这一列，不再铺满视口。
  expect(split!.width / 901).toBeGreaterThan(0.25)
  expect(split!.width / 901).toBeLessThan(0.55)
  const splitSky = await page.locator('.auth-sky').boundingBox()
  expect(splitSky).not.toBeNull()
  expect(splitSky!.width).toBeLessThan(901 * 0.55)
})

test('auth: the sky covers the viewport and nothing overflows on phones', async ({ page }) => {
  // 竖屏背景板铺满 + 四条公开路由零横向溢出，覆盖窄中宽三档手机（320 / 390 / 430）。
  await mockAccountsApi(page, { role: null })
  for (const width of [320, 390, 430]) {
    await page.setViewportSize({ width, height: 844 })
    for (const path of ['/login', '/register', '/forgot-password', '/reset-password']) {
      await page.goto(path)
      await expect(page.locator('.auth-card')).toBeVisible()
      const sky = await page.locator('.auth-sky').boundingBox()
      expect(sky, `${path} @${width} 天空画布缺席`).not.toBeNull()
      expect(sky!.width, `${path} @${width} 天空未铺满`).toBeCloseTo(width, 0)
      const overflowX = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      )
      expect(overflowX, `${path} @${width} 横向溢出`).toBeLessThanOrEqual(0)
    }
  }
})

test('login: the footer and the no-account hint are reachable', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  await expect(page.getByText('还没有账号？')).toBeVisible()
  await expect(page.getByRole('link', { name: '创建一个' })).toHaveAttribute(
    'href',
    '/register',
  )
  await expect(page.getByRole('link', { name: 'GitHub' })).toHaveAttribute(
    'href',
    'https://github.com/frostpolaris1/yiban-auto-sign',
  )
  // 服务端已提供 /terms 与 /privacy 静态页，不再外链仓库 Markdown。
  await expect(page.getByRole('link', { name: '用户协议' })).toHaveAttribute(
    'href',
    '/terms',
  )
  await expect(page.getByRole('link', { name: '隐私政策' })).toHaveAttribute(
    'href',
    '/privacy',
  )
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
  await page.locator('#reset-password').fill(password)
  await page.locator('#reset-confirm').fill(confirmation)
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

  await expect(page.getByRole('alert')).toContainText('请求参数不符合要求，请核对后重试')
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
    await expect(page.locator('#reset-password')).toHaveCount(0)
    expectNoPasswordPosts(api, bad.label)
  })
}

test('reset password: mismatched entries are stopped in the page', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: null })
  await page.goto(`/reset-password?token=${RESET_TOKEN}`)

  // 两枚密码框各自有唯一的显隐按钮名，读屏不会听到两个「显示密码」。
  await expect(page.getByRole('button', { name: '显示新密码' })).toBeVisible()
  await expect(page.getByRole('button', { name: '显示确认密码' })).toBeVisible()

  await submitReset(page, NEW_PASSWORD, `${NEW_PASSWORD}-other`)

  await expect(page.getByText('两次输入的密码不一致')).toBeVisible()
  await expect(page.locator('#reset-confirm')).toBeFocused()
  await expect(page.locator('#reset-password')).toBeVisible()
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

  await expect(page.getByRole('alert')).toContainText('请求参数不符合要求，请核对后重试')
  await expectFocusOnNotice(page, '请求参数不符合要求，请核对后重试')
  await expect(page.locator('#reset-password')).toBeVisible()
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
  await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible()
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

  await page.locator('#reset-password').pressSequentially(NEW_PASSWORD)
  await page.locator('#reset-password').press('Tab')
  await page.locator('#reset-confirm').pressSequentially(NEW_PASSWORD)
  await page.locator('#reset-confirm').press('Enter')

  await expect(page.getByRole('heading', { name: '密码已重置' })).toBeVisible()
  expect(resetPasswordCalls(api), 'Enter submits the form').toHaveLength(1)
})

test('register: pre-login meter uses the local user tier, not the session policy', async ({
  page,
}) => {
  // 契约：password-policy 需登录。注册页未登录不得请求它（其 401 会被判会话过期），
  // 故 meter 吃本地 user 档兜底；即使假 API 提供别的策略也不得生效。
  await mockAccountsApi(page, {
    role: null,
    passwordPolicy: { min_chars: 8, min_classes: 3, max_bytes: 1024 },
  })
  await page.goto('/register')

  await expect(page.getByRole('heading', { name: '创建账号' })).toBeVisible()
  // 空态：只有规则文本（hint），无强度条；提交按钮始终可点，点按会就地给出首错提示。
  await expect(page.getByText('至少 10 位，且包含大写、小写、数字、符号中的 2 类')).toBeVisible()
  await expect(page.locator('.app-pw-meter')).toHaveCount(0)
  const submit = page.getByRole('button', { name: '创建账号', exact: true })
  await expect(submit).toBeEnabled()
  await submit.click()
  await expect(page.locator('#register-email')).toBeFocused()
  await expect(page.locator('#register-email-error')).toBeVisible()
  // 输入后文本让位给强度条；长度 chip 吃本地兜底口径；不足十位提交聚焦口令框且阻断请求。
  await page.locator('#register-email').fill('new@example.com')
  await page.locator('#register-password').fill('abcdefAB')
  await expect(page.locator('.app-pw-meter').getByText('至少 10 位', { exact: true })).toBeVisible()
  await submit.click()
  await expect(page.locator('#register-password')).toBeFocused()
  await expect(page).toHaveURL(/\/register$/)
})

test('register: without a site key renders no widget and no placeholder', async ({ page }) => {
  // 未配置密钥 = 该功能不存在：不渲染控件、不留占位、不占版面，也不请求 Cloudflare。
  await mockAccountsApi(page, { role: null })
  const cloudflareRequests: string[] = []
  page.on('request', (request) => {
    if (request.url().startsWith('https://challenges.cloudflare.com/')) {
      cloudflareRequests.push(request.url())
    }
  })
  await page.goto('/register')

  await expect(page.getByRole('heading', { name: '创建账号' })).toBeVisible()
  await expect(page.locator('[data-turnstile-slot="register"]')).toHaveCount(0)
  await expect(page.getByText('人机验证占位', { exact: true })).toHaveCount(0)
  expect(cloudflareRequests).toEqual([])
})

/**
 * Cloudflare 脚本的替身：不联网，按真实 `render(container, options)` 的形状
 * 往挂载点塞一个按钮，点一下就把令牌交给 `options.callback`。
 * `window.turnstile` 由这段脚本定义，前端就不再请求真实端点。
 */
const TURNSTILE_STUB = `window.__turnstileOptions = null
window.turnstile = {
  render: function (container, options) {
    window.__turnstileOptions = options
    var button = document.createElement('button')
    button.type = 'button'
    button.textContent = 'stub-turnstile'
    button.addEventListener('click', function () { options.callback('stub-token') })
    container.appendChild(button)
    return 'stub-widget'
  },
  remove: function () {},
}`

async function fillRegisterForm(page: Page) {
  await page.locator('#register-email').fill('new@example.com')
  await page.locator('#register-password').fill('Correct-horse-9!')
  await page.locator('#register-confirm').fill('Correct-horse-9!')
  await page.locator('.app-register-agree .box').click()
}

test('register: a configured site key renders the widget and submits its token', async ({
  page,
}) => {
  await mockAccountsApi(page, {
    role: null,
    registration: { turnstile_site_key: 'test-site-key' },
  })
  await page.route('https://challenges.cloudflare.com/turnstile/v0/api.js**', (route) =>
    route.fulfill({ contentType: 'application/javascript', body: TURNSTILE_STUB }),
  )
  let submitted: Record<string, unknown> | null = null
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/api/auth/register')) {
      submitted = request.postDataJSON() as Record<string, unknown>
    }
  })

  await page.goto('/register')
  // 配了站点密钥才挂载控件；同一位置不再有占位。
  await expect(page.locator('[data-turnstile-slot="register"]')).toHaveCount(1)
  const widget = page.getByRole('button', { name: 'stub-turnstile' })
  await expect(widget).toBeVisible()
  // 服务端按 action 绑定令牌：注册控件必须带 register，否则开档必 400。
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __turnstileOptions?: { action?: string } }).__turnstileOptions?.action,
    ),
  ).toBe('register')

  await fillRegisterForm(page)
  await widget.click()
  await page.getByRole('button', { name: '创建账号', exact: true }).click()

  await expect(page).toHaveURL(/\/app$/)
  expect(submitted).not.toBeNull()
  expect(submitted!.turnstile_token).toBe('stub-token')
})

test('register: a failed submit burns the token and forces a fresh verification', async ({
  page,
}) => {
  // 令牌一次性：服务端在 KDF/DB 之前就核销。提交失败后旧令牌已作废，
  // 界面必须换一个新控件，否则用户重试只会拿回 timeout-or-duplicate。
  await mockAccountsApi(page, {
    role: null,
    registration: { turnstile_site_key: 'test-site-key' },
  })
  await page.route('https://challenges.cloudflare.com/turnstile/v0/api.js**', (route) =>
    route.fulfill({
      contentType: 'application/javascript',
      body: `window.__turnstileRenders = 0;
window.turnstile = {
  render: function (container, options) {
    window.__turnstileRenders += 1
    var token = 'token-' + window.__turnstileRenders
    var button = document.createElement('button')
    button.type = 'button'
    button.textContent = 'stub-turnstile'
    button.addEventListener('click', function () { options.callback(token) })
    container.appendChild(button)
    return 'stub-widget'
  },
  remove: function () {},
}`,
    }),
  )
  const tokens: unknown[] = []
  let registers = 0
  await page.route('**/api/auth/register', async (route) => {
    registers += 1
    if (registers === 1) {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ code: 'internal', message: 'boom' }),
      })
      return
    }
    await route.fallback()
  })
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/api/auth/register')) {
      const body = request.postDataJSON() as Record<string, unknown>
      tokens.push(body.turnstile_token)
    }
  })

  await page.goto('/register')
  const widget = page.getByRole('button', { name: 'stub-turnstile' })
  await expect(widget).toBeVisible()
  await fillRegisterForm(page)
  await widget.click()
  await page.getByRole('button', { name: '创建账号', exact: true }).click()

  // 第一次提交失败，页面停住并已重新渲染控件。
  await expect(page.locator('.app-alert-block')).toBeVisible()
  await expect(page).toHaveURL(/\/register$/)
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __turnstileRenders?: number }).__turnstileRenders))
    .toBe(2)

  // 旧令牌已随失败提交作废：不重新过验证再点提交，本地守卫直接拦住。
  await page.getByRole('button', { name: '创建账号', exact: true }).click()
  await expect(page.getByText('请先完成上方的人机验证', { exact: true })).toBeVisible()
  expect(registers).toBe(1)

  // 重新过验证后重试成功，请求里带的是新令牌。
  await widget.click()
  await page.getByRole('button', { name: '创建账号', exact: true }).click()
  await expect(page).toHaveURL(/\/app$/)
  expect(tokens).toEqual(['token-1', 'token-2'])
})

test('register: a configured site key blocks submit until a token arrives', async ({ page }) => {
  await mockAccountsApi(page, {
    role: null,
    registration: { turnstile_site_key: 'test-site-key', turnstile_enforced: true },
  })
  // 这个替身只画一个不会回调的控件：模拟用户还没做完验证。
  await page.route('https://challenges.cloudflare.com/turnstile/v0/api.js**', (route) =>
    route.fulfill({
      contentType: 'application/javascript',
      body: `window.turnstile = { render: function (container) {
        var span = document.createElement('span')
        span.textContent = 'stub-widget'
        container.appendChild(span)
        return 'stub-widget'
      }, remove: function () {} }`,
    }),
  )
  let registers = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/api/auth/register')) registers += 1
  })

  await page.goto('/register')
  await expect(page.getByText('stub-widget', { exact: true })).toBeVisible()
  await fillRegisterForm(page)
  await page.getByRole('button', { name: '创建账号', exact: true }).click()

  await expect(page.getByText('请先完成上方的人机验证', { exact: true })).toBeVisible()
  await expect(page).toHaveURL(/\/register$/)
  expect(registers).toBe(0)
})

test('register: a failed widget script offers an in-page retry instead of a dead form', async ({
  page,
}) => {
  // 脚本加载失败时 UI 不能只剩一句「请先完成验证」：那会让人以为验证码画不出来是
  // 自己的问题，只能刷新整页。这里断言失败态可解释、可就地重试、重试后能提交。
  await mockAccountsApi(page, {
    role: null,
    registration: { turnstile_site_key: 'test-site-key', turnstile_enforced: true },
  })
  let scriptRequests = 0
  await page.route('https://challenges.cloudflare.com/turnstile/v0/api.js**', (route) => {
    scriptRequests += 1
    if (scriptRequests === 1) return route.abort('failed')
    return route.fulfill({
      contentType: 'application/javascript',
      body: `window.turnstile = { render: function (container, options) {
        var button = document.createElement('button')
        button.type = 'button'
        button.textContent = 'stub-turnstile'
        button.addEventListener('click', function () { options.callback('stub-token') })
        container.appendChild(button)
        return 'stub-widget'
      }, remove: function () {} }`,
    })
  })
  let registers = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/api/auth/register')) registers += 1
  })

  await page.goto('/register')
  await expect(page.getByRole('alert')).toContainText('人机验证加载失败')
  await fillRegisterForm(page)

  // 失败态下提交：给的是「加载失败」而不是「请先完成」，并零请求。
  await page.getByRole('button', { name: '创建账号', exact: true }).click()
  await expect(page.getByText('人机验证加载失败，请点上方「重新加载」后再试', { exact: true })).toBeVisible()
  expect(registers).toBe(0)

  // 就地重试，不刷新页面。
  await page.getByRole('button', { name: '重新加载' }).click()
  const widget = page.getByRole('button', { name: 'stub-turnstile' })
  await expect(widget).toBeVisible()
  expect(scriptRequests).toBe(2)
  // 重试成功后「加载失败」必须自己撤掉，不能留到用户过完验证为止。
  await expect(
    page.getByText('人机验证加载失败，请点上方「重新加载」后再试', { exact: true }),
  ).toHaveCount(0)

  await widget.click()
  await page.getByRole('button', { name: '创建账号', exact: true }).click()
  await expect(page).toHaveURL(/\/app$/)
  expect(registers).toBe(1)
})

test('register: enforced without a site key defers to the server instead of a dead guard', async ({
  page,
}) => {
  // enforced 但没有站点密钥 = 配置不完整（或本地快照早于服务端开闸）。本地不能
  // 拿「请先完成上方的人机验证」把表单锁死——上方根本没有控件。放行给服务端判，
  // 服务端回 human_verification_failed 时重拉状态，让控件按新密钥出现。
  await mockAccountsApi(page, {
    role: null,
    registration: { turnstile_site_key: '', turnstile_enforced: true },
  })
  await page.route('**/api/auth/register', (route) =>
    route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({
        error: { code: 'human_verification_failed', message: 'human verification failed' },
      }),
    }),
  )
  let registers = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/api/auth/register')) registers += 1
  })

  await page.goto('/register')
  await fillRegisterForm(page)
  await page.getByRole('button', { name: '创建账号', exact: true }).click()

  await expect(page.getByText('人机验证未通过，请重新完成验证', { exact: true })).toBeVisible()
  await expect(page.getByText('请先完成上方的人机验证', { exact: true })).toHaveCount(0)
  expect(registers).toBe(1)
})

test('register: switching the theme rebuilds the widget in the new palette', async ({
  page,
}) => {
  // Turnstile 的 theme 是 render 时的一次性参数：不重建控件，切主题后 iframe 里仍是旧配色。
  await mockAccountsApi(page, {
    role: null,
    registration: { turnstile_site_key: 'test-site-key' },
  })
  await page.route('https://challenges.cloudflare.com/turnstile/v0/api.js**', (route) =>
    route.fulfill({
      contentType: 'application/javascript',
      body: `window.__turnstileThemes = [];
window.turnstile = { render: function (container, options) {
  window.__turnstileThemes.push(options.theme)
  var span = document.createElement('span')
  span.textContent = 'stub-widget'
  container.appendChild(span)
  return 'stub-' + window.__turnstileThemes.length
}, remove: function () {} }`,
    }),
  )

  await page.goto('/register')
  await expect(page.getByText('stub-widget', { exact: true })).toBeVisible()
  await page.locator('.app-theme-toggle').click()

  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __turnstileThemes: string[] }).__turnstileThemes.length))
    .toBe(2)
  expect(
    await page.evaluate(() => (window as unknown as { __turnstileThemes: string[] }).__turnstileThemes),
  ).toEqual(['light', 'dark'])
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
})

test('register: a site key in log mode renders the widget but never blocks submit', async ({
  page,
}) => {
  await mockAccountsApi(page, {
    role: null,
    registration: { turnstile_site_key: 'test-site-key', turnstile_enforced: false },
  })
  await page.route('https://challenges.cloudflare.com/turnstile/v0/api.js**', (route) =>
    route.fulfill({
      contentType: 'application/javascript',
      body: `window.turnstile = { render: function (container) {
        var span = document.createElement('span')
        span.textContent = 'stub-widget'
        container.appendChild(span)
        return 'stub-widget'
      }, remove: function () {} }`,
    }),
  )
  let registers = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/api/auth/register')) registers += 1
  })

  await page.goto('/register')
  await expect(page.getByText('stub-widget', { exact: true })).toBeVisible()
  await fillRegisterForm(page)
  await page.getByRole('button', { name: '创建账号', exact: true }).click()

  // log 档服务端只记日志：没有令牌也放行，页面不该被本地守卫拦住。
  await expect(page).toHaveURL(/\/app$/)
  expect(registers).toBe(1)
})

test('register: the local fallback governs the meter when the policy endpoint fails', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: null })
  await page.route('**/api/auth/password-policy', (route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'internal', message: 'boom' }),
    }),
  )
  await page.goto('/register')

  // 策略读失败不阻断注册：输入后强度条出现，吃本地兜底口径。
  await page.locator('#register-password').fill('abc')
  await expect(page.locator('.app-pw-meter').getByText('至少 10 位', { exact: true })).toBeVisible()
})

test('register: a 401 policy (session-scoped by contract) does not bounce to login', async ({
  page,
}) => {
  // 真后端契约：policy 需登录。公开注册页未登录时它的 401 不是会话过期，
  // 不得触发全局踢回登录页；未登录不发请求， meter 吃本地兜底。
  await mockAccountsApi(page, { role: null })
  await page.route('**/api/auth/password-policy', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'unauthorized', message: 'unauthorized' }),
    }),
  )
  await page.goto('/register')

  await expect(page.getByRole('heading', { name: '创建账号' })).toBeVisible()
  await page.locator('#register-password').fill('abc')
  await expect(page.locator('.app-pw-meter').getByText('至少 10 位', { exact: true })).toBeVisible()
  await expect(page).toHaveURL(/\/register$/)
})

test('register: a mismatched confirm is flagged while typing', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/register')

  await page.locator('#register-password').fill('Correct-horse-9!')
  await page.locator('#register-confirm').fill('Correct-horse-8!')
  await expect(page.getByText('两次输入的密码不一致')).toBeVisible()
})

test('register: matching confirm shows a done check; unchecked agreement blocks submit with inline error', async ({
  page,
}) => {
  let registered = 0
  await mockAccountsApi(page, { role: null })
  await page.route('**/api/auth/register', (route) => {
    registered += 1
    return route.continue()
  })
  await page.goto('/register')

  await page.locator('#register-email').fill('new@example.com')
  await page.locator('#register-password').fill('Correct-horse-9!')
  await page.locator('#register-confirm').fill('Correct-horse-9!')
  // 匹配即常驻正向完成态。
  await expect(page.locator('#register-confirm-success')).toHaveText('已完成')
  // 协议未勾：点击创建不发请求，就地提示并聚焦勾选框。
  await page.getByRole('button', { name: '创建账号', exact: true }).click()
  expect(registered).toBe(0)
  await expect(page.locator('#register-agree')).toBeFocused()
  await expect(page.locator('#register-agree')).toHaveAttribute('aria-invalid', 'true')
  await expect(page.locator('#register-agree')).toHaveAttribute(
    'aria-describedby',
    'register-agree-error',
  )
  await expect(page.locator('#register-agree-error')).toBeVisible()
  await page.locator('.app-register-agree .box').click()
  await expect(page.locator('#register-agree')).toBeChecked()
  await expect(page.getByRole('button', { name: '创建账号', exact: true })).toBeEnabled()
})

test('register: success signs in and lands the user on /app', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/register')

  await page.locator('#register-email').fill('new@example.com')
  await page.locator('#register-password').fill('Correct-horse-9!')
  await page.locator('#register-confirm').fill('Correct-horse-9!')
  await page.locator('.app-register-agree .box').click()
  await page.getByRole('button', { name: '创建账号', exact: true }).click()

  await expect(page).toHaveURL(/\/app$/)
})

test('register: the strength bar and class chips respond live to the password', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/register')

  // 空口令：强度条不出现（规则由 hint 文本承载）。
  await expect(page.locator('.app-pw-meter')).toHaveCount(0)

  // 短且单类：弱，只有「小写」chip 点亮。
  await page.locator('#register-password').fill('abc')
  await expect(page.locator('.app-pw-meter')).toHaveAttribute('data-score', '1')
  await expect(page.locator('.pw-word')).toHaveText('弱')
  await expect(page.locator('.pw-chip.is-ok')).toHaveCount(1)

  // 长度＋四类齐全：很强，长度与四类共五枚 chip 全亮。
  await page.locator('#register-password').fill('Correct-horse-9!')
  await expect(page.locator('.app-pw-meter')).toHaveAttribute('data-score', '4')
  await expect(page.locator('.pw-word')).toHaveText('很强')
  await expect(page.locator('.pw-chip.is-ok')).toHaveCount(5)
})

test('register: the aside shows three onboarding steps; login keeps the capability list', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: null })
  // 两签文案常驻堆叠（淡入淡出），断言圈定活跃文案，隐藏签不计入。
  const activeCopy = page.locator('.auth-aside-copy[data-active="true"]')
  await page.goto('/register')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('开始自动签到')
  await expect(activeCopy.locator('.auth-steps li')).toHaveCount(3)

  await page.goto('/login')
  await expect(activeCopy.locator('.auth-steps')).toHaveCount(0)
  await expect(activeCopy.locator('.auth-points li')).toHaveCount(4)
})

test('the footer version line opens the server changelog', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  await page.getByRole('button', { name: /易班自动签到 · v\d/ }).click()
  await expect(page.getByRole('dialog')).toContainText('合成条目：登录页改版')

  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('auth: brand name stays high-contrast over daytime sky and key controls meet 44px hit targets', async ({
  page,
}) => {
  await page.clock.setFixedTime(new Date('2026-11-11T12:00:00+08:00'))
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await mockAccountsApi(page, { role: null })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/login')

  // 白昼左栏品牌名不得在亮云上泛白（亮底配深字）。
  const brandColor = await page.evaluate(() => {
    const el = document.querySelector('.auth-brand .name')
    return el ? getComputedStyle(el).color : null
  })
  expect(brandColor).not.toBe('rgb(255, 255, 255)')

  // 主题切换、显隐开关、版本按钮命中区 >= 44px（含 ::after 扩展）。
  const sizes = await page.evaluate(() => {
    const measure = (sel: string) => {
      const el = document.querySelector(sel)
      if (!el) return null
      const r = el.getBoundingClientRect()
      const a = getComputedStyle(el, '::after')
      const hasAfter = a.content !== 'none' && a.content !== ''
      if (!hasAfter) return { w: r.width, h: r.height }
      const aw = parseFloat(a.width) || r.width
      const ah = parseFloat(a.height) || r.height
      return { w: Math.max(r.width, aw), h: Math.max(r.height, ah) }
    }
    return {
      themeToggle: measure('.app-theme-toggle'),
      affix: measure('#login-password ~ .affix-btn'),
      versionBtn: measure('.app-auth-version'),
    }
  })
  expect(sizes.themeToggle?.w).toBeGreaterThanOrEqual(44)
  expect(sizes.themeToggle?.h).toBeGreaterThanOrEqual(44)
  expect(sizes.affix?.h).toBeGreaterThanOrEqual(44)
  expect(sizes.versionBtn?.h).toBeGreaterThanOrEqual(44)
})

test('auth: tablist supports ArrowRight/ArrowLeft switching with aria-controls tabpanel', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  const loginTab = page.getByRole('tab', { name: '登录' })
  const registerTab = page.getByRole('tab', { name: '注册' })
  await expect(loginTab).toHaveAttribute('aria-controls', 'auth-panel-login')
  await loginTab.focus()
  await loginTab.press('ArrowRight')
  await expect(page).toHaveURL(/\/register$/)
  await expect(registerTab).toBeFocused()
  await expect(registerTab).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('#auth-panel-register')).toHaveAttribute('role', 'tabpanel')

  await registerTab.press('ArrowLeft')
  await expect(page).toHaveURL(/\/login$/)
  await expect(loginTab).toBeFocused()
})
