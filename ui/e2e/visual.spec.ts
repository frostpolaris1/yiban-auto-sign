import { test, expect, type Page } from '@playwright/test'
import { mockAccountsApi } from './support/fake-accounts-api'
import { mockDashboardApi } from './support/fake-dashboard-api'
import { SCHEDULE_DAY, mockScheduleApi } from './support/fake-schedule-api'
import { mockSchoolsApi } from './support/fake-schools-api'
import { visibleText } from './support/visible-text'

/**
 * Adminator 壳的视觉回归基线。
 *
 * 矩阵：4 条常驻路由 + 仪表盘两视角，× 2 套主题 × 2 个宽度 = 24 张截图。
 * 仪表盘按角色分视角：user 是签到状态／诊断，admin 是窗口／容量体检。
 * 时间片页按固定业务日另拍（缺省日期会随日历漂移，不能进常驻矩阵）。
 * 另有抽屉／模态／菜单态、无权限页与时间偏好卡等按需快照。
 * 基线随 spec 入库，目录是 `e2e/visual.spec.ts-snapshots/`。
 * 首次运行 `npm run test:visual:update`。此后每次必须 0 差异。
 */

const ROUTES = [
  { path: '/design', name: 'design' },
  { path: '/accounts', name: 'accounts' },
  { path: '/schools', name: 'schools' },
  { path: '/login', name: 'login' },
] as const

const DASHBOARD_VIEWS = [
  { role: 'user', name: 'dashboard-user' },
  { role: 'admin', name: 'dashboard-admin' },
] as const

const VIEWPORTS = [
  { name: '1440', width: 1440, height: 900 },
  { name: '390', width: 390, height: 844 },
] as const

const THEMES = ['light', 'dark'] as const

test.beforeEach(async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })
})

/** 登录页是没有会话的页面。壳内路由需要会话，登录页需要没有会话。 */
async function mockSessionFor(
  page: Page,
  path: string,
  role: 'master' | 'admin' | 'user' = 'master',
) {
  if (path === '/login') {
    await mockAccountsApi(page, { role: null })
    return
  }
  // 时间片页需要窗口、预览与时间偏好端点；路径可能带 `?day=`。
  if (path.startsWith('/schedule')) {
    await mockScheduleApi(page, { role })
    return
  }
  // 仪表盘按角色取数：两视角都要有对应端点，否则基线拍到的会是错误态。
  if (path === '/') {
    await mockDashboardApi(page, { role })
    return
  }
  // 学校页需要完整的学校端点集合：后注册的 route 先匹配，覆盖 beforeEach 的账号 mock。
  if (path === '/schools') {
    await mockSchoolsApi(page, { role })
  }
}

async function openWithTheme(
  page: Page,
  path: string,
  theme: (typeof THEMES)[number],
  role: 'master' | 'admin' | 'user' = 'master',
) {
  await mockSessionFor(page, path, role)
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
    for (const view of DASHBOARD_VIEWS) {
      test(`visual ${view.name} ${theme} ${viewport.name}`, async ({ page }) => {
        await page.setViewportSize({ width: viewport.width, height: viewport.height })
        await openWithTheme(page, '/', theme, view.role)
        // 数据态到位再拍：不等就会拍到「加载中…」。
        // 数字先断言终值再拍：滚动中间帧不得进基线。
        if (view.role === 'user') {
          await expect(page.getByRole('table', { name: '今日签到状态' })).toContainText(
            '账号 #1',
          )
          await expect
            .poll(() =>
              visibleText(
                page.locator('.kpi-card').filter({ hasText: '账号总数' }).locator('.kpi-value'),
              ),
            )
            .toBe('4个')
        } else {
          await expect(page.getByLabel('任务')).toHaveValue('school-alpha:task-daily')
          await expect(page.locator('.app-metric')).toHaveCount(3)
          await expect
            .poll(() => visibleText(page.locator('.app-metric').filter({ hasText: '容量' })))
            .toBe('容量40')
        }
        await expect(page).toHaveScreenshot(`${view.name}-${theme}-${viewport.name}.png`, {
          fullPage: true,
        })
      })
    }

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

  test(`visual accounts row menu ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    // master：位置项置灰带原因，正好钉住禁用原因的可读性。
    await page.getByRole('button', { name: '更多操作 138****8000' }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    await expect(page).toHaveScreenshot(`accounts-rowmenu-${theme}-1440.png`)
  })

  test(`visual accounts edit modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '更多操作 138****8000' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /编辑/ }).click()
    await expect(page.getByRole('dialog', { name: '编辑账号？' })).toBeVisible()
    await expect(page).toHaveScreenshot(`accounts-edit-${theme}-1440.png`)
  })

  test(`visual accounts location modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    // 位置策略只归 user/admin（beforeEach 是 master，会置灰打不开）：
    // 叠一层 user 角色的 mock，后注册的 route 先匹配。
    await mockAccountsApi(page, { role: 'user' })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '更多操作 139****9000' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /位置/ }).click()
    await expect(page.getByRole('dialog', { name: '设置位置策略？' })).toBeVisible()
    await expect(page).toHaveScreenshot(`accounts-location-${theme}-1440.png`)
  })

  test(`visual accounts delete modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '更多操作 135****0001' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /删除/ }).click()
    await expect(page.getByRole('dialog', { name: '删除账号？' })).toBeVisible()
    await expect(page).toHaveScreenshot(`accounts-delete-${theme}-1440.png`)
  })
}

for (const theme of THEMES) {
  test(`visual toast ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '暂停 139****9000' }).click()
    await page
      .getByRole('dialog', { name: '暂停账号？' })
      .getByRole('button', { name: '确认暂停' })
      .click()
    const toast = page.locator('[data-sonner-toast]')
    await expect(toast).toBeVisible()
    await expect(toast).toContainText('已暂停 139****9000')
    await expect(page).toHaveScreenshot(`toast-${theme}-1440.png`)
  })
}

for (const theme of THEMES) {
  test(`visual schools drawer ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme)
    await page.getByRole('button', { name: '查看 合成学校甲' }).click()
    await expect(page.getByRole('dialog', { name: '学校详情' })).toBeVisible()
    await expect(page).toHaveScreenshot(`schools-drawer-${theme}-1440.png`)
  })

  test(`visual schools create modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme)
    await page.getByRole('button', { name: '新建学校' }).click()
    await expect(page.getByRole('dialog', { name: '新建学校？' })).toBeVisible()
    await expect(page).toHaveScreenshot(`schools-create-${theme}-1440.png`)
  })

  test(`visual schools edit modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme)
    await page.getByRole('button', { name: '更多操作 合成学校甲' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /编辑/ }).click()
    await expect(page.getByRole('dialog', { name: '编辑学校？' })).toBeVisible()
    await expect(page).toHaveScreenshot(`schools-edit-${theme}-1440.png`)
  })

  test(`visual schools admins modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme)
    await page.getByRole('button', { name: '更多操作 合成学校甲' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /管理员/ }).click()
    await expect(page.getByRole('dialog', { name: '学校管理员' })).toBeVisible()
    await expect(page.getByText('admin-one@example.com')).toBeVisible()
    await expect(page).toHaveScreenshot(`schools-admins-${theme}-1440.png`)
  })

  test(`visual schools admin row menu ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    // admin 打开行菜单：编辑与管理员两项置灰带原因，正好钉住禁用原因的可读性。
    await openWithTheme(page, '/schools', theme, 'admin')
    await page.getByRole('button', { name: '更多操作 合成学校甲' }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    await expect(page).toHaveScreenshot(`schools-rowmenu-admin-${theme}-1440.png`)
  })

  test(`visual schools denied ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme, 'user')
    await expect(page.getByText('你的角色无权查看学校管理')).toBeVisible()
    await expect(page).toHaveScreenshot(`schools-denied-${theme}-1440.png`)
  })
}

for (const theme of THEMES) {
  test(`visual schedule page ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, `/schedule?day=${SCHEDULE_DAY}`, theme)
    await expect(page.getByRole('table', { name: '时间片窗口' })).toBeVisible()
    await expect(page).toHaveScreenshot(`schedule-${theme}-1440.png`, { fullPage: true })
  })

  test(`visual schedule preview ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, `/schedule?day=${SCHEDULE_DAY}`, theme)
    await page.getByRole('button', { name: '查看计划 合成学校甲' }).click()
    const dialog = page.getByRole('dialog', { name: '计划预览' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('窗口版本')
    await expect(dialog).toContainText('v0')
    await expect
      .poll(() => visibleText(dialog.locator('.app-metric').nth(0)))
      .toBe('容量40')
    await expect(page).toHaveScreenshot(`schedule-preview-${theme}-1440.png`)
  })

  test(`visual schedule override ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, `/schedule?day=${SCHEDULE_DAY}`, theme)
    await page.getByRole('button', { name: '查看计划 合成学校甲' }).click()
    await page.getByRole('dialog', { name: '计划预览' }).getByRole('button', { name: '写入覆盖' }).click()
    const dialog = page.getByRole('dialog', { name: '写入窗口覆盖？' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByLabel('开始时间')).toHaveValue('2026-10-28T08:10')
    await expect(page).toHaveScreenshot(`schedule-override-${theme}-1440.png`)
  })

  test(`visual schedule denied ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, `/schedule?day=${SCHEDULE_DAY}`, theme, 'user')
    await expect(page.getByText('你的角色无权查看时间片管理')).toBeVisible()
    await expect(page).toHaveScreenshot(`schedule-denied-${theme}-1440.png`, { fullPage: true })
  })

  test(`visual dashboard time preference card ${theme}`, async ({ page }) => {
    // 偏好卡只对 user 渲染：走 user 视角的看板取数面（含时间偏好端点）。
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/', theme, 'user')
    await expect(page.locator('.app-pref-current')).toHaveText('窗口开始 + 15 分钟')
    await expect
      .poll(() =>
        visibleText(
          page.locator('.kpi-card').filter({ hasText: '账号总数' }).locator('.kpi-value'),
        ),
      )
      .toBe('4个')
    await expect(page).toHaveScreenshot(`dashboard-time-pref-${theme}-1440.png`, {
      fullPage: true,
    })
  })
}

for (const theme of THEMES) {
  test(`visual profile menu ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/', theme)
    await page.getByRole('button', { name: '账号菜单' }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    await expect(page).toHaveScreenshot(`profile-menu-${theme}-1440.png`)
  })

  test(`visual session expired ${theme}`, async ({ page }) => {
    // 有会话进账号页，列表请求返回 401：真实用户路径，落到登录页并带过期提示。
    await mockAccountsApi(page, { role: 'master', expireOn: ['/api/accounts'] })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await expect(page.getByText('会话已过期，请重新登录')).toBeVisible()
    await expect(page).toHaveScreenshot(`session-expired-${theme}-1440.png`)
  })
}

test('no horizontal overflow at either width, in either theme', async ({ page }) => {
  // 时间片页不进口径矩阵（缺省日期会漂移），但溢出判据一样要钉：固定业务日补拍。
  const paths = [...ROUTES.map((route) => route.path), `/schedule?day=${SCHEDULE_DAY}`]
  for (const theme of THEMES) {
    for (const path of paths) {
      await page.setViewportSize({ width: 390, height: 844 })
      await openWithTheme(page, path, theme)
      const narrow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      expect(
        narrow.scrollWidth,
        `${path} overflows at 390px in ${theme}: ${narrow.scrollWidth} > ${narrow.clientWidth}`,
      ).toBeLessThanOrEqual(narrow.clientWidth + 1)

      await page.setViewportSize({ width: 1440, height: 900 })
      const wide = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      expect(
        wide.scrollWidth,
        `${path} overflows at 1440px in ${theme}: ${wide.scrollWidth} > ${wide.clientWidth}`,
      ).toBeLessThanOrEqual(wide.clientWidth + 1)
    }
  }
})
