import { test, expect, type Page } from '@playwright/test'
import { mockAccountsApi } from './support/fake-accounts-api'

/**
 * Adminator 壳的视觉回归基线。
 *
 * 矩阵：4 条路由 × 2 套主题 × 2 个宽度 = 16 张截图。
 * 另有账号页抽屉／模态各 2 张，共 20 张。
 * 基线随 spec 入库，目录是 `e2e/visual.spec.ts-snapshots/`。
 * 首次运行 `npm run test:visual:update`。此后每次必须 0 差异。
 */

const ROUTES = [
  { path: '/', name: 'dashboard' },
  { path: '/design', name: 'design' },
  { path: '/accounts', name: 'accounts' },
  { path: '/login', name: 'login' },
] as const

const VIEWPORTS = [
  { name: '1440', width: 1440, height: 900 },
  { name: '390', width: 390, height: 844 },
] as const

const THEMES = ['light', 'dark'] as const

test.beforeEach(async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })
})

async function openWithTheme(page: Page, path: string, theme: (typeof THEMES)[number]) {
  await page.addInitScript((value) => {
    try {
      localStorage.setItem('dash26-theme', value)
    } catch {
      /* localStorage 可能被禁用。引导脚本会退回媒体查询。 */
    }
  }, theme)
  await page.goto(path)
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
  await page.evaluate(() => document.fonts.ready)
  await page.waitForLoadState('networkidle')
  // 首帧的字体回退与行盒可能再稳定一帧。截图前等一下，避免 1px 位移。
  await page.waitForTimeout(200)
}

for (const theme of THEMES) {
  for (const viewport of VIEWPORTS) {
    for (const route of ROUTES) {
      test(`visual ${route.name} ${theme} ${viewport.name}`, async ({ page }) => {
        await page.setViewportSize({ width: viewport.width, height: viewport.height })
        await openWithTheme(page, route.path, theme)
        await expect(page).toHaveScreenshot(`${route.name}-${theme}-${viewport.name}.png`, {
          fullPage: true,
        })
      })
    }
  }
}

for (const theme of THEMES) {
  test(`visual accounts drawer ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '查看 138****8000' }).click()
    await expect(page.getByRole('dialog', { name: '账号详情' })).toBeVisible()
    await expect(page).toHaveScreenshot(`accounts-drawer-${theme}-1440.png`)
  })

  test(`visual accounts modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '暂停 139****9000' }).click()
    await expect(page.getByRole('dialog', { name: '暂停账号？' })).toBeVisible()
    await expect(page).toHaveScreenshot(`accounts-modal-${theme}-1440.png`)
  })
}

test('no horizontal overflow at either width, in either theme', async ({ page }) => {
  for (const theme of THEMES) {
    for (const route of ROUTES) {
      await page.setViewportSize({ width: 390, height: 844 })
      await openWithTheme(page, route.path, theme)
      const narrow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      expect(
        narrow.scrollWidth,
        `${route.path} overflows at 390px in ${theme}: ${narrow.scrollWidth} > ${narrow.clientWidth}`,
      ).toBeLessThanOrEqual(narrow.clientWidth + 1)

      await page.setViewportSize({ width: 1440, height: 900 })
      const wide = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      expect(
        wide.scrollWidth,
        `${route.path} overflows at 1440px in ${theme}: ${wide.scrollWidth} > ${wide.clientWidth}`,
      ).toBeLessThanOrEqual(wide.clientWidth + 1)
    }
  }
})
