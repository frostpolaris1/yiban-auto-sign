import { test, expect } from '@playwright/test'

/**
 * User-visible behavior of the shell and the component gallery.
 * These tests assert outcomes, not internal choreography.
 */

test('theme is applied before the first paint and survives a reload', async ({ page, context }) => {
  await page.addInitScript(() => {
    localStorage.setItem('dash26-theme', 'dark')
    const target = window as unknown as { __themeAtFirstPaint?: string | null }
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (
            entry.name === 'first-contentful-paint' &&
            target.__themeAtFirstPaint === undefined
          ) {
            target.__themeAtFirstPaint = document.documentElement.getAttribute('data-theme')
          }
        }
      }).observe({ type: 'paint', buffered: true })
    } catch {
      /* PerformanceObserver is optional; the attribute assertions below still run. */
    }
  })
  await page.goto('/')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')

  const atFirstPaint = await page
    .waitForFunction(
      () => (window as unknown as { __themeAtFirstPaint?: string | null }).__themeAtFirstPaint !== undefined,
      undefined,
      { timeout: 5_000 },
    )
    .then(() =>
      page.evaluate(
        () => (window as unknown as { __themeAtFirstPaint?: string | null }).__themeAtFirstPaint ?? null,
      ),
    )
  expect(atFirstPaint, 'data-theme must be set before the first contentful paint').toBe('dark')

  await page.locator('#themeToggle').click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

  const keys = await page.evaluate(() => Object.keys(localStorage).sort())
  expect(keys, 'localStorage must hold the theme key only').toEqual(['dash26-theme'])

  // A fresh tab in the same context reads the persisted value. Do not reload
  // this page: its init script would re-seed the dark value.
  const second = await context.newPage()
  await second.goto('/')
  await expect(second.locator('html')).toHaveAttribute('data-theme', 'light')
  await second.close()
})

test('mobile drawer opens from the hamburger and closes on Escape', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')

  const hamburger = page.locator('[data-drawer-open]')
  await expect(hamburger).toBeVisible()
  await hamburger.click()
  await expect(page.locator('body')).toHaveClass(/has-drawer-open/)
  await expect(page.locator('.d-sidebar')).toBeInViewport()

  await page.keyboard.press('Escape')
  await expect(page.locator('body')).not.toHaveClass(/has-drawer-open/)
})

test('navigation moves between shell routes and marks the active item', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.d-sidebar a.is-active')).toContainText('仪表盘')

  await page.locator('.d-sidebar').getByRole('button', { name: '设计系统' }).click()
  await page.locator('.d-sidebar').getByRole('link', { name: '组件画廊' }).click()
  await expect(page).toHaveURL(/\/design$/)
  await expect(page.locator('.d-sidebar a.is-active')).toContainText('组件画廊')

  await page.locator('.d-sidebar').getByRole('link', { name: '仪表盘' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.locator('.d-sidebar a.is-active')).toContainText('仪表盘')
})

test('gallery: modal opens, traps focus, and closes', async ({ page }) => {
  await page.goto('/design')

  await page.getByRole('button', { name: '打开弹窗' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('删除账号？')

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
})

test('gallery: dropdown menu opens and selects an item', async ({ page }) => {
  await page.goto('/design')

  await page.getByRole('button', { name: '账号操作' }).click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  await menu.getByRole('menuitem', { name: '账号设置' }).click()
  await expect(menu).toBeHidden()
})

test('gallery: tabs switch the visible panel', async ({ page }) => {
  await page.goto('/design')

  await expect(page.getByRole('tabpanel')).toContainText('概览面板')
  await page.getByRole('tab', { name: '活动' }).click()
  await expect(page.getByRole('tabpanel')).toContainText('活动面板')
  await expect(page.getByRole('tab', { name: '活动' })).toHaveAttribute('aria-selected', 'true')
})

test('gallery: switch toggles with mouse and keyboard', async ({ page }) => {
  await page.goto('/design')

  const toggle = page.getByRole('switch', { name: '启用双因子认证' })
  await expect(toggle).toHaveAttribute('data-state', 'unchecked')
  await toggle.click()
  await expect(toggle).toHaveAttribute('data-state', 'checked')
  await toggle.press('Space')
  await expect(toggle).toHaveAttribute('data-state', 'unchecked')
})

test('gallery: tooltip appears on hover', async ({ page }) => {
  await page.goto('/design')

  await page.getByRole('button', { name: '悬停查看提示' }).hover()
  await expect(page.getByRole('tooltip')).toBeVisible()
  await expect(page.getByRole('tooltip')).toContainText('签到窗口 06:30')
})

test('gallery: dismissible alert disappears', async ({ page }) => {
  await page.goto('/design')

  const alert = page.getByRole('status').filter({ hasText: '保留期提醒' })
  await expect(alert).toBeVisible()
  await alert.getByRole('button', { name: '关闭提示' }).click()
  await expect(alert).toHaveCount(0)
})

test('login form stays local and does not navigate', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: '登录' })).toBeVisible()

  await page.getByLabel('邮箱').fill('operator@example.com')
  await page.getByLabel('密码').fill('not-a-real-password')
  await page.getByRole('button', { name: '登录控制台' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByText('本页为静态表单')).toBeVisible()
})
