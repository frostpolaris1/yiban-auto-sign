import { test, expect, type Locator, type Page } from '@playwright/test'
import { ACCOUNTS, mockAccountsApi, RESET_TOKEN } from './support/fake-accounts-api'
import { mockAuditApi } from './support/fake-audit-api'
import { mockDashboardApi } from './support/fake-dashboard-api'
import { SCHEDULE_DAY, mockScheduleApi } from './support/fake-schedule-api'
import {
  ENGINE_VISUAL_SNAPSHOT,
  VISUAL_NOW,
  installFakeEventSource,
  mockLogsApi,
} from './support/fake-logs-api'
import { mockQueueApi } from './support/fake-queue-api'
import { mockNotifyApi, visualNotifications } from './support/fake-notify-api'
import { mockSchoolsApi } from './support/fake-schools-api'
import { mockUsersApi } from './support/fake-users-api'
import { mockUserAppApi } from './support/fake-user-app-api'
import { visibleText } from './support/visible-text'

/**
 * Adminator 壳的视觉回归基线。
 *
 * 矩阵：14 条常驻路由（含 `/logs` 两页签与固定业务日 `/system?day=...`，页内 Date 冻结在
 * VISUAL_NOW，快照时刻全部派生自它）与看板（admin），
 * × 2 套主题 × 2 个宽度；另有抽屉／模态／菜单态与溢出判据。
 * 看板只归 master/admin（窗口／容量体检）：user 被壳层 302 到 `/app`，
 * 用户端界面由本文件末尾独立的 user app describe 覆盖，不再有 dashboard-user 视角。
 * 系统设置按固定业务日入常驻矩阵（缺省日期会随日历漂移）。
 * 基线目录 `e2e/visual.spec.ts-snapshots/`，每平台百余张（现行实数见目录计数）；
 * 无权限页快照已随 user 视角退役（user 进不了管理台路由，权限门留在行为用例里）。
 * 溢出判据跑全部常驻路由（覆盖 `/` 的 master 形态）。
 * 基线随 spec 入库，目录是 `e2e/visual.spec.ts-snapshots/`。
 * 首次运行 `npm run test:visual:update`。此后每次必须 0 差异。
 */

const ROUTES = [
  { path: '/queue', name: 'queue' },
  { path: '/audit', name: 'audit' },
  { path: '/logs?tab=events', name: 'logs-events' },
  { path: '/logs?tab=patrol', name: 'logs-patrol' },
  { path: '/design', name: 'design' },
  { path: '/settings', name: 'settings' },
  { path: `/system?day=${SCHEDULE_DAY}`, name: 'system' },
  { path: '/accounts', name: 'accounts' },
  { path: '/schools', name: 'schools' },
  { path: '/users', name: 'users' },
  { path: '/login', name: 'login' },
  { path: '/register', name: 'register' },
  { path: '/forgot-password', name: 'forgot-password' },
  { path: `/reset-password?token=${RESET_TOKEN}`, name: 'reset-password' },
] as const

// 管理台看板只归 master/admin：user 一律被壳层 302 到 /app，
// 用户端界面由本文件末尾独立的 user app describe 覆盖，不再有 dashboard-user 视角。
const DASHBOARD_VIEWS = [{ role: 'admin', name: 'dashboard-admin' }] as const

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
  if (
    path === '/login' ||
    path === '/register' ||
    path.startsWith('/forgot-password') ||
    path.startsWith('/reset-password')
  ) {
    await mockAccountsApi(page, { role: null })
    return
  }
  // 系统设置·签到计划分区需要窗口、预览与时间偏好端点；路径可能带 `?day=`。
  if (path.startsWith('/schedule') || path.startsWith('/system')) {
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
    return
  }
  // 用户管理页只归 master；独立的假 API 也覆盖 `/api/auth/me`。
  if (path === '/users') {
    await mockUsersApi(page, { role })
  }
  // 签到台账（签到事件 + 运行巡检）：SSE 用假体钉住「实时」态，
  // 页内 Date 冻结在 VISUAL_NOW（2030-01-01），事件页签缺省业务日与巡检页签租约剩余跨日不漂。
  if (path.startsWith('/logs')) {
    await page.clock.setFixedTime(new Date(VISUAL_NOW))
    await installFakeEventSource(page)
    await mockLogsApi(page, { role, snapshot: ENGINE_VISUAL_SNAPSHOT })
  }
  // 待办页的业务日取当前日期：同样冻结在 VISUAL_NOW 避免跨日基线漂移，并钉住 SSE 实时态。
  if (path === '/queue') {
    await page.clock.setFixedTime(new Date(VISUAL_NOW))
    await installFakeEventSource(page)
    await mockQueueApi(page, { role })
  }
  // 审计页：固定合成台账与巡检结论，不依赖时钟。
  if (path === '/audit') {
    await mockAuditApi(page, { role })
  }
}

async function openWithTheme(
  page: Page,
  path: string,
  theme: (typeof THEMES)[number],
  role: 'master' | 'admin' | 'user' = 'master',
  stopSky = false,
) {
  await mockSessionFor(page, path, role)
  const authPage =
    path === '/login' ||
    path === '/register' ||
    path.startsWith('/forgot-password') ||
    path.startsWith('/reset-password')
  if (authPage || stopSky) {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.clock.setFixedTime(new Date('2026-11-11T12:00:00+08:00'))
  }
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
  } else if (path === '/users') {
    await expect(page.getByRole('table', { name: '用户列表' })).toContainText(
      'master-one@example.com',
    )
  } else if (path.startsWith('/schedule')) {
    await expect(page.getByRole('table', { name: '时间片窗口' })).toContainText('合成学校甲')
  } else if (path.startsWith('/system')) {
    // 系统设置要等四张策略卡 + 时间片表各自取数；4 worker 抢 CPU 时 5s 会不够（曾误报成加载中）。
    await expect(page.getByRole('table', { name: '时间片窗口' })).toContainText('合成学校甲', {
      timeout: 15000,
    })
    await expect(page.locator('#policy-registration-card')).toContainText('允许新用户公开注册')
    await expect(page.locator('#policy-auto-approve-card')).toContainText('开启新号自动审核')
    await expect(page.locator('#policy-quota-card')).toContainText('全站活跃用户上限')
    await expect(page.locator('#policy-schedule-card')).toContainText('前边界缩进')
    await expect(page.locator('#announcement')).toContainText('示例公告')
    await expect(page.locator('#channels')).toContainText('测试邮件')
    // 04d 两分区：数据锚点防骨架/错误态伪基线。
    await expect(page.locator('#roster')).toContainText('YIBAN_MAIL_PASS')
    await expect(page.locator('#version')).toContainText('进程在应答')
  } else if (path === '/logs?tab=events') {
    await expect(page.getByRole('region', { name: '泳道 合成学校甲' })).toBeVisible()
  } else if (path === '/queue') {
    await expect(page.locator('[data-queue-group="pending"]')).toContainText('138****8000')
    await expect(page.locator('[data-queue-group="failed"]')).toContainText('138****8003')
    await expect(page.locator('[data-queue-group="unsigned"]')).toContainText('138****8004')
    await expectNumbers(page.locator('[data-queue-summary] .q-tele-value .app-number'), ['3', '1', '1'])
  } else if (path === '/audit') {
    // 账本六行与页头完好结论都到位再拍；总数徽标与 fixtures 同源。
    await expect(page.locator('[data-audit-row]')).toHaveCount(6)
    await expect(page.locator('[data-audit-verdict]')).toContainText('链完好')
  } else if (path === '/logs?tab=patrol') {
    // 数据态到位：四指标与两条在飞行渲染完成再拍。
    await expect(page.locator('dl', { hasText: '待执行' })).toContainText('3')
    await expect(page.locator('.engine-lease').first()).toContainText('时')
  } else if (path === '/') {
    await expect(page.locator('.kpi-card')).toHaveCount(4)
  } else if (path === '/design') {
    await expect(page.getByRole('heading', { name: /组件画廊/ })).toBeVisible()
  } else if (path === '/settings') {
    // 会话与矩阵到位：账号信息卡读到会话夹具，改密卡在表单态，通知订阅 4×3 矩阵已渲染。
    const card = page.locator('.card').filter({ hasText: '账号信息' })
    await expect(card).toContainText('operator@example.com')
    await expect(page.getByLabel(/^当前密码/)).toBeVisible()
    await expect(page.getByRole('switch', { name: '凭据失效 · 站内信' })).toBeVisible()
  } else if (path === '/register') {
    await expect(page.getByRole('heading', { name: '创建账号' })).toBeVisible()
    await expect(page.locator('.auth-steps li')).toHaveCount(3)
  } else if (path === '/forgot-password') {
    await expect(page.getByLabel('邮箱')).toBeVisible()
  } else if (path.startsWith('/reset-password')) {
    // 令牌合法：页面停在表单态，不是「链接不完整」态。
    await expect(page.locator('#reset-password')).toBeVisible()
  }
}

/** 等页面级动画全部落定。
 *
 * Playwright 的 `animations: 'disabled'` 是在截图那一刻把**已在册**的动画快进到终点；
 * 若弹窗/抽屉的入场动画在那一刻之后才被 React 挂上（`both` 填充），截到的就是中间帧
 * ——学校新建弹窗曾因此拍到 130 万像素的半透明叠影（背后列表整行透出来）。
 * 无限循环的装饰动画（骨架脉冲等）不算未落定。
 */
async function waitForAnimations(page: Page) {
  await page.waitForFunction(() =>
    document
      .getAnimations()
      .every(
        (animation) =>
          animation.playState !== 'running' ||
          animation.effect?.getTiming().iterations === Infinity,
      ),
  )
  // React 可能在上面的放行之后才挂上入场动画（弹层 mount 晚于内容断言），再盯两眼。
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const busy = await page.evaluate(() =>
      document.getAnimations().some((animation) => animation.playState === 'running'),
    )
    if (!busy) break
    await page.waitForTimeout(50)
  }
  // 半透明中间帧：弹层/菜单必须真的画到 opacity 1 才算落地（学校新建弹窗曾拍到叠影）。
  await page
    .waitForFunction(
      () =>
        Array.from(
          document.querySelectorAll<HTMLElement>(
            '[role="dialog"],[role="alertdialog"],[role="menu"]',
          ),
        ).every((el) => getComputedStyle(el).opacity === '1'),
      undefined,
      { timeout: 2000 },
    )
    .catch(() => undefined)
  // 动画落定后再抹平横向滚动位：按钮获焦会让 .table-scroll 自己滚一小段，
  // 弹层背后那半屏表格就此左右错开（曾让 accounts 的 5 张快照随机差几十像素）。
  await settleScroll(page)
  // fullPage 截图以 document scrollHeight 为准。页面总高带亚像素（设计页实测 2606.90625）
  // 时高度会在 2607/2608 之间翻，Playwright 把这 1px 记成「两张截图不稳定」。等两次采样
  // 一致再放行；连续 20 次都不一致说明真的在抖，交给断言照常报错。
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const before = await page.evaluate(() => document.documentElement.scrollHeight)
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    )
    const after = await page.evaluate(() => document.documentElement.scrollHeight)
    if (before === after) break
  }
  await settleWindowTop(page)
}

/** 把窗口滚回文档原点并等合成层跟上。
 *
 * Playwright 点击前会把触发器滚进视口，文档就停在下方；Chromium 的
 * `Page.captureScreenshot` 以**文档原点**为剪辑、与滚动位置无关，于是固定定位的弹层
 * 会在成图里整体下移 scrollY（system-preview 曾差 27 万像素、页脚被裁）。
 * 这段必须放在全部等待之后：焦点作用域、React 重挂、Radix 焦点还原都可能在
 * 中途把文档再滚下去，早做一遍等于没做。
 */
async function settleWindowTop(page: Page) {
  // 反复归零直到真的在顶部：每次归零都可能被排在其后的 scrollIntoView 推翻。
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const offset = await page.evaluate(() => {
      if (window.scrollY === 0 && window.scrollX === 0) return 0
      window.scrollTo({ top: 0, left: 0, behavior: 'instant' })
      return 1
    })
    if (offset === 0) break
    await page.waitForTimeout(50)
  }
  // 固定定位层在独立合成层上，scroll 跳变后要几帧才跟上。
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
  )
  await page.waitForTimeout(150)
  // 先拍一张丢弃：旧滚动位合成出的错位帧会先出一次，第二张才是最终状态。
  await page.screenshot({ animations: 'disabled', caret: 'hide' })
}

/** 点开首屏之外的触发器，但不让文档滚下去。
 *
 * Playwright 的 click 会先把触发器滚进视口，文档就停在下方；Radix 的滚动锁把这个位置
 * 记成「打开时的位置」，之后任何一次重新加锁都会把文档滚回去 —— 快照于是在滚动位与
 * 原点之间摇摆（system-preview 在 CI 上差 25466 像素，本地快机器上则一次都不复现）。
 * 改用 DOM click：不滚动、不改焦点，React 侧回调与真实点击一致。
 */
async function clickWithoutScroll(locator: Locator) {
  await locator.evaluate((el: HTMLElement) => el.click())
}

/** 把页面内横向滚动位归零。
 *
 * 行内按钮获焦后浏览器会把最近的可滚容器滚到按钮可见处，落点随时序（平滑滚动 + 列表重挂），
 * 弹层背后的表格列会因此左右错开几十像素 —— 快照就随机差一截。弹层已把背景置为 inert，
 * 用户看不到这次归零，故截图前统一抹平。
 */
async function settleScroll(page: Page) {
  await page.evaluate(async () => {
    const zero = () => {
      for (const el of Array.from(document.querySelectorAll<HTMLElement>('.table-scroll'))) {
        if (el.scrollLeft !== 0) el.scrollLeft = 0
      }
    }
    zero()
    // 归零会被排在其后的 scrollIntoView 推翻（焦点环、Radix 焦点作用域都走 rAF），
    // 故连抹若干帧；不改 overflow —— 那会连横向滚动条一起吞掉，基线就不再是产品实况。
    for (let frame = 0; frame < 12; frame += 1) {
      await new Promise((resolve) => requestAnimationFrame(resolve))
      zero()
    }
  })
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
        await expect(page.getByLabel('任务')).toContainText('合成学校甲 · 每日签到')
        // 容量三指标只数容量卡：运行状态卡另有自己的 metric，不再全局计数。
        const capacity = page.locator('.card').filter({ hasText: '容量体检' })
        await expect(capacity.locator('.app-metric')).toHaveCount(3)
        await expectNumbers(capacity.locator('.app-metric'), ['容量40', '需求12', '缺口0'])
        // 四张 KPI：成功/失败/进行中/待处理；总览不再展示成功率卡。
        await expectNumbers(page.locator('.kpi-card .kpi-value'), [
          '3个',
          '0个',
          '1个',
          '3个',
        ])
        await waitForAnimations(page)
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
        await waitForAnimations(page)
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
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`accounts-drawer-${theme}-1440.png`)
  })

  test(`visual accounts drawer with photos ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    // 照片区只对 user/admin 渲染（契约 x-roles），故叠一层 admin 会话。
    await mockAccountsApi(page, { role: 'admin' })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '查看 138****8000' }).click()
    await expect(page.getByRole('dialog', { name: '账号详情' })).toBeVisible()
    await expect(page.getByRole('region', { name: '账号照片' })).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`accounts-drawer-photos-${theme}-1440.png`)
  })

  test(`visual accounts modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '暂停 139****9000' }).click()
    await expect(page.getByRole('dialog', { name: '暂停账号？' })).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`accounts-modal-${theme}-1440.png`)
  })

  test(`visual accounts row menu ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    // master：位置项置灰带原因，正好钉住禁用原因的可读性。
    await page.getByRole('button', { name: '更多操作 138****8000' }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    // 与 profile menu 同纪律：等入场动画结束，快照不带中间帧。
    await page.waitForTimeout(250)
    await waitForAnimations(page)
    // 行 hover 底会随列表重挂随机丢失：指针挪到中性点，快照只收稳态行。
    await page.mouse.move(8, 8)
    await expect(page).toHaveScreenshot(`accounts-rowmenu-${theme}-1440.png`)
  })

  test(`visual accounts edit modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '更多操作 138****8000' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /编辑/ }).click()
    await expect(page.getByRole('dialog', { name: '编辑账号？' })).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`accounts-edit-${theme}-1440.png`)
  })

  test(`visual accounts location modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    // 位置策略只归 user/admin（beforeEach 是 master，会置灰打不开）。
    // user 已被壳层 302 到 /app、进不了 /accounts，这里改用 admin 会话打开。
    await mockAccountsApi(page, { role: 'admin' })
    await openWithTheme(page, '/accounts', theme)
    // 换夹具后列表会重取一次：先钉住数据态，否则背后表格停在骨架/空态，弹窗本体却没变。
    await expectData(page, '/accounts')
    await page.getByRole('button', { name: '更多操作 139****9000' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /位置/ }).click()
    await expect(page.getByRole('dialog', { name: '设置位置策略？' })).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`accounts-location-${theme}-1440.png`)
  })

  test(`visual accounts delete modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme)
    await page.getByRole('button', { name: '更多操作 135****0001' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /删除/ }).click()
    await expect(page.getByRole('dialog', { name: '删除账号？' })).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`accounts-delete-${theme}-1440.png`)
  })
}

for (const theme of THEMES) {
  test(`visual nav badge ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    // 专属用例：把三行改成 2 行待审 + 1 行已拒，徽标与提示条同为一个 3。
    await mockAccountsApi(page, {
      role: 'master',
      accounts: [
        { ...ACCOUNTS[0], status: 'pending' },
        { ...ACCOUNTS[1], status: 'rejected' },
        { ...ACCOUNTS[2], status: 'pending' },
      ],
    })
    await openWithTheme(page, '/accounts', theme)
    await expectData(page, '/accounts')
    const link = page.getByRole('link', { name: '账号，待处理账号 3' })
    await expect(link.locator('.nav-badge')).toHaveText('3')
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`nav-badge-${theme}-1440.png`, { fullPage: true })
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
    // 暂停确认后行内说明变宽，Linux 字体下账号表在 .table-scroll 内横向溢出。
    // 点击/焦点会给该容器留下 scrollLeft，截图前归零，避免与基线偏差。
    await page.locator('.table-scroll').evaluate((el) => {
      el.scrollLeft = 0
    })
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`toast-${theme}-1440.png`)
  })
}

for (const theme of THEMES) {
  test(`visual schools drawer ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme)
    await page.getByRole('button', { name: '查看 合成学校甲' }).click()
    await expect(page.getByRole('dialog', { name: '学校详情' })).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`schools-drawer-${theme}-1440.png`)
  })

  test(`visual schools drawer with default photo ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    // 缺省图区只对 admin 渲染：master 能改引用却读不到字节。
    await openWithTheme(page, '/schools', theme, 'admin')
    await page.getByRole('button', { name: '查看 合成学校甲' }).click()
    await expect(page.getByRole('dialog', { name: '学校详情' })).toBeVisible()
    await expect(page.getByRole('region', { name: '学校缺省图' })).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`schools-drawer-photos-${theme}-1440.png`)
  })

  test(`visual schools create modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme)
    await page.getByRole('button', { name: '新建学校' }).click()
    await expect(page.getByRole('dialog', { name: '新建学校？' })).toBeVisible()
    // 入场动画（app-modal-in 160ms，scale 0.97→1 + 淡入）未落定时截图会拍到叠影。
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`schools-create-${theme}-1440.png`)
  })

  test(`visual schools edit modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme)
    await page.getByRole('button', { name: '更多操作 合成学校甲' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /编辑/ }).click()
    await expect(page.getByRole('dialog', { name: '编辑学校？' })).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`schools-edit-${theme}-1440.png`)
  })

  test(`visual schools admins modal ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/schools', theme)
    await page.getByRole('button', { name: '更多操作 合成学校甲' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /管理员/ }).click()
    await expect(page.getByRole('dialog', { name: '学校管理员' })).toBeVisible()
    await expect(page.getByText('admin-one@example.com')).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`schools-admins-${theme}-1440.png`)
  })

  test(`visual schools admin row menu ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    // admin 打开行菜单：编辑与管理员两项置灰带原因，正好钉住禁用原因的可读性。
    await openWithTheme(page, '/schools', theme, 'admin')
    await page.getByRole('button', { name: '更多操作 合成学校甲' }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    await waitForAnimations(page)
    // 同 accounts row menu：不留按钮 hover 底，快照口径与行态解耦。
    await page.mouse.move(8, 8)
    await expect(page).toHaveScreenshot(`schools-rowmenu-admin-${theme}-1440.png`)
  })
}

for (const theme of THEMES) {
  test(`visual system preview ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, `/system?day=${SCHEDULE_DAY}`, theme)
    // 背景层同样要数据态：弹窗本体逐像素稳定，飘的是身后骨架→数据重排的那几帧。
    await expectData(page, '/system')
    // 触发器在首屏之下：真实点击会把文档滚到 278px，弹层是固定布局，截图就随滚动位漂移。
    // 换成不滚动的点击，「文档全程在顶部」成了前置条件，不靠截图前归零去补救。
    await clickWithoutScroll(page.getByRole('button', { name: '查看计划 合成学校甲' }))
    const dialog = page.getByRole('dialog', { name: '计划预览' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('窗口版本')
    await expect(dialog).toContainText('v0')
    // 三个数字并行滚动：三格都落定再拍，避免高负载下拍到中间帧。
    await expectNumbers(dialog.locator('.app-metric'), ['容量40', '需求12', '缺口0'])
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`system-preview-${theme}-1440.png`)
  })

  test(`visual system override ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, `/system?day=${SCHEDULE_DAY}`, theme)
    // 背景层同样要数据态：弹窗本体逐像素稳定，飘的是身后骨架→数据重排的那几帧。
    await expectData(page, '/system')
    await clickWithoutScroll(page.getByRole('button', { name: '查看计划 合成学校甲' }))
    await page.getByRole('dialog', { name: '计划预览' }).getByRole('button', { name: '写入覆盖' }).click()
    const dialog = page.getByRole('dialog', { name: '写入窗口覆盖？' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByLabel('开始时间')).toHaveValue('2026-10-28T08:10')
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`system-override-${theme}-1440.png`)
  })
}

for (const theme of THEMES) {
  test(`visual profile menu ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/', theme)
    // 背景是看板：滚动数字全部落定再拍，菜单态不能带中间帧。
    await expect(page.getByLabel('任务')).toContainText('合成学校甲 · 每日签到')
    const capacity = page.locator('.card').filter({ hasText: '容量体检' })
    await expectNumbers(capacity.locator('.app-metric'), ['容量40', '需求12', '缺口0'])
    await expectNumbers(page.locator('.kpi-card .kpi-value'), ['3个', '0个', '1个', '3个'])
    await page.getByRole('button', { name: '账号菜单' }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    // 菜单入场动画会改变整页的合成层，进而改变文字抗锯齿；等它结束再拍。
    await page.waitForTimeout(200)
    await waitForAnimations(page)
    // 头像按钮的 hover 底同样不该进基线：指针退到中性点再拍。
    await page.mouse.move(8, 8)
    await expect(page).toHaveScreenshot(`profile-menu-${theme}-1440.png`)
  })

  test(`visual session expired ${theme}`, async ({ page }) => {
    // 有会话进账号页，列表请求返回 401：真实用户路径，落到登录页并带过期提示。
    await mockAccountsApi(page, { role: 'master', expireOn: ['/api/accounts'] })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openWithTheme(page, '/accounts', theme, 'master', true)
    await expect(page.getByText('会话已过期，请重新登录')).toBeVisible()
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`session-expired-${theme}-1440.png`)
  })

  test(`visual inbox drawer ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.clock.setFixedTime(new Date(VISUAL_NOW))
    await mockNotifyApi(page, {
      role: 'master',
      notifications: visualNotifications(VISUAL_NOW),
    })
    await openWithTheme(page, '/design', theme)
    await expectData(page, '/design')
    const bell = page.locator('.topbar-actions').getByRole('button', { name: /通知/ })
    await expect(bell.locator('.count')).toHaveText('3')
    await bell.click()
    const drawer = page.getByRole('dialog', { name: '通知收件箱' })
    await expect(drawer).toBeVisible()
    await expect(drawer.locator('.app-inbox-item')).toHaveCount(4)
    await expect(drawer).toContainText('5 分钟前')
    await expect(drawer).toContainText('昨天 18:20')
    await page.waitForFunction(() =>
      document
        .getAnimations()
        .every(
          (animation) =>
            animation.playState !== 'running' ||
            animation.effect?.getTiming().iterations === Infinity,
        ),
    )
    await waitForAnimations(page)
    await expect(page).toHaveScreenshot(`inbox-drawer-${theme}-1440.png`)
  })
}

const USER_VIEWPORTS = [
  { name: '390', width: 390, height: 844 },
  { name: '768', width: 768, height: 1024 },
  { name: '1024', width: 1024, height: 900 },
  { name: '1440', width: 1440, height: 900 },
] as const

const USER_APP_VIEWS = [
  { path: '/app', name: 'home' },
  { path: '/app/history?day=2026-11-10', name: 'history' },
  { path: '/app/settings', name: 'settings' },
  { path: '/app/account', name: 'account' },
  { path: '/app/account/new', name: 'account-new' },
] as const

test.describe('user app', () => {
  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date('2026-11-11T08:00:00+08:00'))
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await mockUserAppApi(page, { role: 'user' })
  })

  for (const theme of THEMES) {
    for (const view of USER_APP_VIEWS) {
      const viewports =
        view.name === 'home'
          ? USER_VIEWPORTS
          : USER_VIEWPORTS.filter((item) => item.name === '390' || item.name === '1440')
      for (const viewport of viewports) {
        test(`user app ${view.name} ${viewport.name} ${theme}`, async ({ page }) => {
          await page.setViewportSize({ width: viewport.width, height: viewport.height })
          await page.addInitScript((value) => {
            try {
              localStorage.setItem('dash26-theme', value)
            } catch {
              /* localStorage 可能被禁用。 */
            }
          }, theme)
          await page.goto(view.path)
          await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
          await page.evaluate(() => document.fonts.ready)
          const landed =
            view.name === 'account-new'
              ? page.getByRole('dialog').getByRole('heading')
              : page.getByRole('heading', { level: 1 })
          await expect(landed).toBeVisible()
          await waitForAnimations(page)
          await expect(page).toHaveScreenshot(
            `user-app-${view.name}-${viewport.name}-${theme}.png`,
            { fullPage: true },
          )
        })
      }
    }
  }
})

test('user app has no horizontal overflow at four widths', async ({ page }) => {
  for (const viewport of USER_VIEWPORTS) {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await page.clock.setFixedTime(new Date('2026-11-11T08:00:00+08:00'))
    await mockUserAppApi(page, { role: 'user' })
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await page.goto('/app')
    await expect(page.locator('.uapp-hero-title')).toBeVisible()
    const overflow = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      inner: window.innerWidth,
    }))
    expect(overflow.scroll, viewport.name).toBe(overflow.inner)
  }
})

test('no horizontal overflow at either width, in either theme', async ({ page }) => {
  // 看板只归 master/admin：`/` 用 master 泛扫，user 视角已随壳层收敛退役。
  const targets = ROUTES.map((route) => ({ path: route.path, role: 'master' as const }))
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

