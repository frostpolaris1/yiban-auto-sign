import { test, expect, type Page } from '@playwright/test'

/**
 * 壳与组件画廊的用户可见行为。
 * 断言结果，不钉内部调用顺序。
 */

async function focusIsInsideDialog(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const active = document.activeElement
    const dialog = document.querySelector('[role="dialog"]')
    return Boolean(active && dialog && dialog.contains(active))
  })
}

test('theme is applied before the first paint and survives a reload', async ({ page, context }) => {
  await page.addInitScript(() => {
    localStorage.setItem('dash26-theme', 'dark')
    const probe: { value?: string; time?: number; fcp?: number; sets: number } = { sets: 0 }
    ;(window as unknown as { __themeProbe?: typeof probe }).__themeProbe = probe

    // 记录第一次 `data-theme` 写入及其时刻。之后不再读 DOM：
    // 探针保存当时发生的事，不是属性此刻的值。
    const original = Element.prototype.setAttribute
    Element.prototype.setAttribute = function (this: Element, name: string, value: string) {
      if (name === 'data-theme' && this === document.documentElement && probe.sets === 0) {
        probe.value = String(value)
        probe.time = performance.now()
        probe.sets += 1
      }
      return original.call(this, name, value)
    }

    // 浏览器发出首次内容绘制时，记下时刻。
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry.name === 'first-contentful-paint' && probe.fcp === undefined) {
            probe.fcp = entry.startTime
          }
        }
      }).observe({ type: 'paint', buffered: true })
    } catch {
      /* 拿不到 FCP 条目时，下面的断言会明确失败。 */
    }
  })
  await page.goto('/')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')

  await page.waitForFunction(
    () =>
      (window as unknown as { __themeProbe?: { fcp?: number } }).__themeProbe?.fcp !== undefined,
    undefined,
    { timeout: 5_000 },
  )
  const firstPaint = await page.evaluate(() => {
    const probe = (window as unknown as { __themeProbe?: { value?: string; time?: number; fcp?: number } })
      .__themeProbe
    return {
      value: probe?.value ?? null,
      time: probe?.time ?? null,
      fcp: probe?.fcp ?? null,
    }
  })
  expect(firstPaint.value, 'first data-theme value written to <html>').toBe('dark')
  expect(firstPaint.time, 'time of the first data-theme write').not.toBeNull()
  expect(firstPaint.fcp, 'first-contentful-paint timestamp').not.toBeNull()
  expect(
    firstPaint.time as number,
    'data-theme must be written before the first contentful paint',
  ).toBeLessThanOrEqual(firstPaint.fcp as number)

  await page.locator('#themeToggle').click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

  const keys = await page.evaluate(() => Object.keys(localStorage).sort())
  expect(keys, 'localStorage must hold the theme key only').toEqual(['dash26-theme'])

  // 同上下文的新标签页读到持久化值。不要重载本页：
  // 它的 init 脚本会重新种入深色值。
  const second = await context.newPage()
  await second.goto('/')
  await expect(second.locator('html')).toHaveAttribute('data-theme', 'light')
  await second.close()
})

test('mobile drawer opens from the hamburger and closes on Escape', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')

  const hamburger = page.locator('[data-drawer-open]')
  const sidebar = page.locator('.d-sidebar')
  await expect(hamburger).toBeVisible()
  await expect(hamburger, 'the drawer starts closed').toHaveAttribute('aria-expanded', 'false')
  await expect(sidebar, 'a closed drawer is outside the viewport').not.toBeInViewport()

  await hamburger.click()
  await expect(hamburger, 'the drawer reports the open state').toHaveAttribute(
    'aria-expanded',
    'true',
  )
  await expect(sidebar, 'an open drawer is visible').toBeInViewport()

  await page.keyboard.press('Escape')
  await expect(hamburger, 'Escape closes the drawer').toHaveAttribute('aria-expanded', 'false')
  await expect(sidebar, 'a closed drawer leaves the viewport').not.toBeInViewport()
})

test('navigation moves between shell routes and marks the active item', async ({ page }) => {
  await page.goto('/')

  const activeLink = page.locator('.d-sidebar a[aria-current="page"]')
  await expect(activeLink).toContainText('仪表盘')

  const group = page.locator('.d-sidebar').getByRole('button', { name: '设计系统' })
  await expect(group, 'the group starts collapsed').toHaveAttribute('aria-expanded', 'false')
  await group.click()
  await expect(group, 'the group reports the expanded state').toHaveAttribute(
    'aria-expanded',
    'true',
  )

  await page.locator('.d-sidebar').getByRole('link', { name: '组件画廊' }).click()
  await expect(page).toHaveURL(/\/design$/)
  await expect(activeLink).toContainText('组件画廊')

  await page.locator('.d-sidebar').getByRole('link', { name: '仪表盘' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(activeLink).toContainText('仪表盘')
})

test('gallery: modal traps focus and closes on Escape', async ({ page }) => {
  await page.goto('/design')

  await page.getByRole('button', { name: '打开弹窗' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('删除账号？')
  await expect(await focusIsInsideDialog(page), 'focus starts inside the dialog').toBe(true)

  for (let step = 1; step <= 10; step += 1) {
    await page.keyboard.press('Tab')
    expect(await focusIsInsideDialog(page), `focus left the dialog after Tab ${step}`).toBe(true)
  }
  for (let step = 1; step <= 4; step += 1) {
    await page.keyboard.press('Shift+Tab')
    expect(await focusIsInsideDialog(page), `focus left the dialog after Shift+Tab ${step}`).toBe(
      true,
    )
  }

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

test('login form stays local and offers no long-lived session option', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: '登录' })).toBeVisible()

  const form = page.locator('.auth-form')
  await expect(form.locator('input[type="checkbox"]'), 'no persistence checkbox').toHaveCount(0)
  await expect(page.getByText(/保持登录/), 'no 30-day session promise').toHaveCount(0)

  await page.getByLabel('邮箱').fill('operator@example.com')
  await page.getByLabel('密码').fill('not-a-real-password')
  await page.getByRole('button', { name: '登录控制台' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByText('本页为静态表单')).toBeVisible()
})
