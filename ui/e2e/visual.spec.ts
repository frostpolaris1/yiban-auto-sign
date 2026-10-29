import { test, expect, type Locator, type Page } from '@playwright/test'
import { mockAccountsApi, RESET_TOKEN } from './support/fake-accounts-api'
import { mockDashboardApi } from './support/fake-dashboard-api'
import { SCHEDULE_DAY, mockScheduleApi } from './support/fake-schedule-api'
import { mockSchoolsApi } from './support/fake-schools-api'
import { visibleText } from './support/visible-text'

/**
 * Adminator 壳的视觉回归基线。
 *
 * 矩阵：7 条常驻路由 + 密码找回两页 + 仪表盘两视角，
 * × 2 套主题 × 2 个宽度 = 36 张截图。
 * 仪表盘按角色分视角：user 是签到状态／诊断，admin 是窗口／容量体检。
 * 时间片页按固定业务日另拍（缺省日期会随日历漂移，不能进常驻矩阵），
 * 补 1440 与 390 两个宽度。
 * 另有抽屉／模态／菜单态、无权限页与时间偏好卡等按需快照。
 * 溢出判据跑全部常驻路由与时间片页；`/` 另跑 user 视角（另一套布局）。
 * 基线随 spec 入库，目录是 `e2e/visual.spec.ts-snapshots/`。
 * 首次运行 `npm run test:visual:update`。此后每次必须 0 差异。
 */

const ROUTES = [
  { path: '/design', name: 'design' },
  { path: '/settings', name: 'settings' },
  { path: '/accounts', name: 'accounts' },
  { path: '/schools', name: 'schools' },
  { path: '/login', name: 'login' },
  { path: '/forgot-password', name: 'forgot-password' },
  { path: `/reset-password?token=${RESET_TOKEN}`, name: 'reset-password' },
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

/** 登录与密码找回三页都没有会话。壳内路由需要会话，这几页需要没有会话。 */
async function mockSessionFor(
  page: Page,
  path: string,
  role: 'master' | 'admin' | 'user' = 'master',
) {
  if (path === '/login' || path.startsWith('/forgot-password') || path.startsWith('/reset-password')) {
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
  // 字体到位是截图的必要条件；数据态由各用例自己断言之后再拍。
  await page.evaluate(() => document.fonts.ready)
}

// 视觉基线在「有动效」口径下拍摄；行为用例的全局减少动效不适用于本文件。
test.use({ reducedMotion: 'no-preference' })

/** 数据锚点：截图或量宽度之前，先确认页面拿到的是数据态，不是骨架。 */
async function expectData(page: Page, path: string) {
  if (path === '/accounts') {
    await expect(page.getByRole('table', { name: '账号列表' })).toContainText('138****8000')
  } else if (path === '/schools') {
    await expect(page.getByRole('table', { name: '学校列表' })).toContainText('合成学校甲')
  } else if (path.startsWith('/schedule')) {
    await expect(page.getByRole('table', { name: '时间片窗口' })).toContainText('合成学校甲')
  } else if (path === '/') {
    await expect(page.locator('.kpi-card')).toHaveCount(4)
  } else if (path === '/design') {
    await expect(page.getByRole('heading', { name: /组件画廊/ })).toBeVisible()
  } else if (path === '/settings') {
    // 会话到位：账号信息卡读到会话夹具，改密卡在表单态。
    const card = page.locator('.card').filter({ hasText: '账号信息' })
    await expect(card).toContainText('operator@example.com')
    await expect(page.getByLabel(/^当前密码/)).toBeVisible()
  } else if (path === '/forgot-password') {
    await expect(page.getByLabel('邮箱')).toBeVisible()
  } else if (path.startsWith('/reset-password')) {
    // 令牌合法：页面停在表单态，不是「链接不完整」态。
    await expect(page.getByLabel('新密码')).toBeVisible()
  }
}

/** 等一组滚动数字全部落定：逐个断言可见终值，避免拍到中间帧。 */
async function expectNumbers(scope: Locator, expected: readonly string[]) {
  // 个数也要钉：新增一格而没写期望值时，这里会红而不是静默跳过。
  await expect(scope).toHaveCount(expected.length)
  for (let index = 0; index < expected.length; index += 1) {
    await expect.poll(() => visibleText(scope.nth(index))).toBe(expected[index])
  }
  // 入场动画（KPI 卡 rise-in、表格 fade-in）未结束时截图，整页文字会用灰度抗锯齿，
  // 与动画结束后的基线差一层。等页面级动画全部落定再交回调用方。
  await scope
    .page()
    .waitForFunction(() =>
      document
        .getAnimations()
        .every(
          (animation) =>
            animation.playState !== 'running' ||
            animation.effect?.getTiming().iterations === Infinity,
        ),
    )
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
          await expectNumbers(page.locator('.kpi-card .kpi-value'), ['4个', '1个', '1个', '1个'])
        } else {
          await expect(page.getByLabel('任务')).toHaveValue('school-alpha:task-daily')
          await expect(page.locator('.app-metric')).toHaveCount(3)
          await expectNumbers(page.locator('.app-metric'), ['容量40', '需求12', '缺口0'])
          await expectNumbers(page.locator('.kpi-card .kpi-value'), ['4项', '3项', '1项', '2项'])
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
        await expectData(page, route.path)
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
    const table = page.getByRole('table', { name: '时间片窗口' })
    await expect(table).toBeVisible()
    await expect(table).toContainText('合成学校甲')
    await expect(page).toHaveScreenshot(`schedule-${theme}-1440.png`, { fullPage: true })
  })

  test(`visual schedule narrow ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await openWithTheme(page, `/schedule?day=${SCHEDULE_DAY}`, theme)
    // 表格在取数前就以「加载中…」占位存在：等首行数据到位再拍。
    const table = page.getByRole('table', { name: '时间片窗口' })
    await expect(table).toBeVisible()
    await expect(table).toContainText('合成学校甲')
    await expect(page).toHaveScreenshot(`schedule-${theme}-390.png`, { fullPage: true })
  })

  test(`visual schedule preview ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, `/schedule?day=${SCHEDULE_DAY}`, theme)
    await page.getByRole('button', { name: '查看计划 合成学校甲' }).click()
    const dialog = page.getByRole('dialog', { name: '计划预览' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('窗口版本')
    await expect(dialog).toContainText('v0')
    // 三个数字并行滚动：三格都落定再拍，避免高负载下拍到中间帧。
    await expectNumbers(dialog.locator('.app-metric'), ['容量40', '需求12', '缺口0'])
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
    await expectNumbers(page.locator('.kpi-card .kpi-value'), ['4个', '1个', '1个', '1个'])
    await expect(page).toHaveScreenshot(`dashboard-time-pref-${theme}-1440.png`, {
      fullPage: true,
    })
  })
}

for (const theme of THEMES) {
  test(`visual profile menu ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/', theme)
    // 背景是看板：滚动数字全部落定再拍，菜单态不能带中间帧。
    await expect(page.getByLabel('任务')).toHaveValue('school-alpha:task-daily')
    await expectNumbers(page.locator('.app-metric'), ['容量40', '需求12', '缺口0'])
    await expectNumbers(page.locator('.kpi-card .kpi-value'), ['4项', '3项', '1项', '2项'])
    await page.getByRole('button', { name: '账号菜单' }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    // 菜单入场动画会改变整页的合成层，进而改变文字抗锯齿；等它结束再拍。
    await page.waitForTimeout(200)
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
  // 看板的 user 视角是另一套布局：`/` 追加 user 一遍；其余路由 master 即可。
  const targets = [
    ...ROUTES.map((route) => ({ path: route.path, role: 'master' as const })),
    { path: `/schedule?day=${SCHEDULE_DAY}`, role: 'master' as const },
    { path: '/', role: 'user' as const },
  ]
  for (const theme of THEMES) {
    for (const { path, role } of targets) {
      // mock 的 catch-all 是后注册优先：不清路由会让上一目标的兜底吞掉本目标的请求。
      // 清完先补基础会话 mock（/design 与 /accounts 都依赖它），具体页 mock 后注册覆盖。
      await page.unrouteAll({ behavior: 'ignoreErrors' })
      await mockAccountsApi(page, { role })
      await page.setViewportSize({ width: 390, height: 844 })
      await openWithTheme(page, path, theme, role)
      await expectData(page, path)
      const narrow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      expect(
        narrow.scrollWidth,
        `${role} ${path} overflows at 390px in ${theme}: ${narrow.scrollWidth} > ${narrow.clientWidth}`,
      ).toBeLessThanOrEqual(narrow.clientWidth + 1)

      await page.setViewportSize({ width: 1440, height: 900 })
      const wide = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      expect(
        wide.scrollWidth,
        `${role} ${path} overflows at 1440px in ${theme}: ${wide.scrollWidth} > ${wide.clientWidth}`,
      ).toBeLessThanOrEqual(wide.clientWidth + 1)
    }
  }
})
