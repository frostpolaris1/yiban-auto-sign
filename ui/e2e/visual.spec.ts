import { test, expect, type Page } from '@playwright/test'

/**
 * Visual regression baseline for the Adminator shell.
 *
 * Matrix: 3 routes x 2 themes x 2 widths = 12 screenshots.
 * Baselines are committed under `e2e/visual.spec.ts-snapshots/`.
 * First run: `npm run test:visual:update`. Later runs must report 0 diff.
 */

const ROUTES = [
  { path: '/', name: 'dashboard' },
  { path: '/design', name: 'design' },
  { path: '/login', name: 'login' },
] as const

const VIEWPORTS = [
  { name: '1440', width: 1440, height: 900 },
  { name: '390', width: 390, height: 844 },
] as const

const THEMES = ['light', 'dark'] as const

async function openWithTheme(page: Page, path: string, theme: (typeof THEMES)[number]) {
  await page.addInitScript((value) => {
    try {
      localStorage.setItem('dash26-theme', value)
    } catch {
      /* localStorage can be blocked; the boot script falls back to the media query. */
    }
  }, theme)
  await page.goto(path)
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
  await page.evaluate(() => document.fonts.ready)
  await page.waitForLoadState('networkidle')
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
