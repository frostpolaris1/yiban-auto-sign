import { test, expect, type Page } from '@playwright/test'
import {
  SESSION_CSRF,
  SESSION_EMAIL,
  SESSION_PASSWORD,
  changePasswordCalls,
  mockAccountsApi,
} from './support/fake-accounts-api'

/**
 * 账号设置页与登录态改密的不变式。
 *
 * 模拟的真实用户动作：登录用户打开顶栏「账号设置」，看一眼自己的邮箱与角色，
 * 再填当前口令、新口令、确认新口令并提交。
 * 断言用户可见结果：toast 文案、提示条消失、字段清空、错误文案、请求体形状。
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
  // 401 是旧口令错，不是会话过期：留在本页，不回登录页，不显示过期提示。
  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByText('会话已过期，请重新登录')).toHaveCount(0)
  // 后端信封原文不得直接展示：界面用固定文案。
  await expect(page.getByText('invalid email or password')).toHaveCount(0)
  await expect(page.getByLabel(/^当前密码/)).toHaveValue(WRONG_CURRENT)
  expect(changePasswordCalls(api), 'the request was attempted once').toHaveLength(1)
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
  await page.getByLabel('密码').fill(SESSION_PASSWORD)
  await page.getByRole('button', { name: '登录控制台' }).click()

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
  await expect(sidebar.getByRole('link', { name: '账号设置' })).toBeVisible()

  await page.getByRole('button', { name: '账号菜单' }).click()
  const menu = page.getByRole('menu')
  await expect(menu.getByRole('menuitem', { name: '个人信息' })).toHaveCount(0)
  await menu.getByRole('menuitem', { name: '账号设置' }).click()
  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByRole('heading', { name: '修改密码' })).toBeVisible()
})
