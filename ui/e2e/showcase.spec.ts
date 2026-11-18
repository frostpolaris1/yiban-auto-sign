import { test, expect, type Page } from '@playwright/test'
import { visibleText } from './support/visible-text'

/**
 * 展示模式的不变式。编号与用例名一致。
 *
 * invariant 7：`VITE_SHOWCASE=1` 构建下，看板走合成会话（master）与合成夹具：
 * 卡片有数据，且零 `/api/` 请求（会话探针也不发）。
 * 该构建由 4174 端口的预览服务提供；构建命令见 `package.json` 的 `build:showcase`。
 *
 * invariant 6：微交互在展示构建里照常装配：toast 容器在 DOM 里，数字有夹具终值，
 * 列表过渡不抛错。展示机没有写操作桩，成功 toast 的端到端触发不在此测
 * （写请求会打到不存在的后端），这条已在汇报里登记为未测维度。
 *
 * invariant 8：展示构建只读。账号、学校、时间片三页的写入口一律禁用，界面可见
 * `展示模式只读`。读操作照常：筛选、翻页、详情抽屉、管理员列表、计划预览。
 * 写入口漏一个，访客就会打到静态站的 SPA 回落并看到 JSON 解析错误。
 * 禁用按钮本身不派发 click；唯一提交按钮被禁用时，Chromium 也不派发回车隐式提交。
 * 设置页另用 `requestSubmit()` 走同一条 submit 事件路径，直接验证 onSubmit 的只读守卫。
 * 零请求断言是兜底，真正的判据是 disabled、文案与守卫。
 *
 * invariant 9：用户管理页取合成夹具，列出三个角色的用户，零 `/api/` 请求。
 * 本页只读，没有写入口；筛选与翻页照常可用。
 *
 * invariant 10：账号抽屉的照片区在展示构建里显示合成占位，零 `/api/` 请求。
 * 照片区在正式构建只对 user/admin 渲染；展示会话是 master，故靠展示分支放行。
 *
 * invariant 12：审计日志页取合成台账与巡检结论，筛选项与详情抽屉照常可读，零 `/api/` 请求。
 */

/** 界面上的只读原因。断言用户可见文本，不导入实现常量。 */
const READ_ONLY_REASON = '展示模式只读'

/** 收集页面发出的 `/api/` 路径。展示构建一条都不该有。 */
function trackApiRequests(page: Page): string[] {
  const requests: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname.startsWith('/api/')) requests.push(url.pathname)
  })
  return requests
}

test.use({ baseURL: 'http://127.0.0.1:4174' })

test('invariant 7: the showcase dashboard renders fixtures with zero requests', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('签到运行')

  // 精确匹配标签：`今日成功` 是 `今日成功率` 的子串。
  const total = page
    .locator('.kpi-card')
    .filter({ has: page.getByText('今日成功', { exact: true }) })
  await expect.poll(() => visibleText(total.locator('.kpi-value'))).toBe('3个')

  const card = page.locator('.card').filter({ hasText: '容量体检' })
  // 容量卡改用 Radix Select：触发器是按钮，断言可见文案而非原生 value。
  await expect(page.getByLabel('任务')).toContainText('合成学校甲 · 每日点名')
  await expect
    .poll(() => visibleText(card.locator('.app-metric').filter({ hasText: '容量' })))
    .toBe('容量24')
  await expect
    .poll(() => visibleText(card.locator('.app-metric').filter({ hasText: '需求' })))
    .toBe('需求8')

  // 展示构建直接读合成 summary：徽标与合成数据同值，仍不发请求。
  const accountsLink = page.getByRole('link', { name: '账号，待处理账号 2' })
  await expect(accountsLink.locator('.nav-badge')).toHaveText('2')
  expect(
    apiRequests.filter((path) => path === '/api/accounts/summary'),
    'the showcase badge reads no summary request',
  ).toHaveLength(0)

  expect(apiRequests, 'the showcase build never touches the API').toEqual([])
})

test('invariant 6: the showcase build mounts the toast host and reports no page error', async ({
  page,
}) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))

  await page.goto('/accounts')
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()
  // 无 toast 时 sonner 只渲染常驻的实时区域；列表容器随首条 toast 挂载。
  await expect(page.locator('section[aria-live="polite"]')).toHaveCount(1)
  expect(errors, 'the animated table and the toast host raise no page error').toEqual([])
})

test('invariant 8: every account write entry is disabled with the read-only reason', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/accounts')
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()

  // 页头「新建账号」也是写入口：禁用并给只读原因（M3）。
  const create = page.getByRole('button', { name: '新建账号' })
  await expect(create).toBeDisabled()
  await expect(create).toHaveAttribute('title', READ_ONLY_REASON)

  const pendingRow = page.locator('.app-account-row').filter({ hasText: '138****8000' }).first()
  for (const name of ['签到 138****8000', '验证 138****8000', '评审 138****8000']) {
    const button = pendingRow.getByRole('button', { name })
    await expect(button).toBeDisabled()
    await expect(button).toHaveAttribute('title', READ_ONLY_REASON)
    await button.click({ force: true })
  }
  await expect(pendingRow.locator('.app-deny-note')).toHaveText(READ_ONLY_REASON)

  // 暂停与恢复也是写入口，与上面同族。
  const activeRow = page.locator('.app-account-row').filter({ hasText: '139****9000' }).first()
  const pause = activeRow.getByRole('button', { name: '暂停 139****9000' })
  await expect(pause).toBeDisabled()
  await expect(pause).toHaveAttribute('title', READ_ONLY_REASON)
  await pause.click({ force: true })
  await expect(activeRow.locator('.app-deny-note')).toHaveText(READ_ONLY_REASON)

  // 读操作照常：筛选控件可用。
  await expect(page.getByLabel('账号筛选')).toBeEnabled()
  await expect(page.getByLabel('状态筛选')).toBeEnabled()
  await expect(page.getByLabel('学校筛选')).toBeEnabled()

  // 行菜单三项同样禁用并给原因。
  await pendingRow.getByRole('button', { name: '更多操作 138****8000' }).click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  for (const name of ['编辑', '位置', '删除']) {
    const item = menu.getByRole('menuitem', { name: new RegExp(name) })
    await expect(item).toHaveAttribute('aria-disabled', 'true')
    await expect(item.locator('.dd-item-reason')).toHaveText(READ_ONLY_REASON)
  }
  await menu.getByRole('menuitem', { name: /编辑/ }).click({ force: true })

  expect(apiRequests, 'a read-only showcase sends no account write').toEqual([])
})

test('invariant 8: the schools page blocks create and edit while the admins list opens read-only', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/schools')
  await expect(page.getByRole('table', { name: '学校列表' })).toBeVisible()

  const create = page.getByRole('button', { name: '新建学校' })
  await expect(create).toBeDisabled()
  await expect(create).toHaveAttribute('title', READ_ONLY_REASON)
  await expect(page.locator('.app-page-actions .app-deny-note')).toHaveText(READ_ONLY_REASON)
  await create.click({ force: true })

  // 读操作照常：详情仍可打开。
  await page.getByRole('button', { name: '查看 合成学校甲' }).click()
  await expect(page.getByRole('dialog', { name: '学校详情' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '学校详情' })).toHaveCount(0)

  await page.getByRole('button', { name: '更多操作 合成学校甲' }).click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  const edit = menu.getByRole('menuitem', { name: /编辑/ })
  await expect(edit).toHaveAttribute('aria-disabled', 'true')
  await expect(edit.locator('.dd-item-reason')).toHaveText(READ_ONLY_REASON)
  await edit.click({ force: true })

  // 管理员入口是读：列表照常打开，模态内的添加与移除才置灰。
  await menu.getByRole('menuitem', { name: /管理员/ }).click()
  const admins = page.getByRole('dialog', { name: '学校管理员' })
  await expect(admins).toBeVisible()
  await expect(admins).toContainText('admin-one@example.com')
  await expect(admins.getByRole('button', { name: '添加管理员' })).toBeDisabled()
  await expect(admins.getByRole('button', { name: '移除 admin-one@example.com' })).toBeDisabled()
  await expect(admins.locator('.app-deny-note').first()).toHaveText(READ_ONLY_REASON)
  await admins.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(admins).toHaveCount(0)

  // 禁用按钮的零请求断言是兜底：真正的判据是上面的 disabled 与文案。
  expect(apiRequests, 'a read-only showcase sends no school write').toEqual([])
})

test('invariant 8: the schedule preview opens read-only and blocks both write buttons', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/schedule')
  await expect(page.getByRole('table', { name: '时间片窗口' })).toBeVisible()

  await page.getByRole('button', { name: '查看计划 合成学校甲' }).first().click()
  const dialog = page.getByRole('dialog', { name: '计划预览' })
  await expect(dialog).toBeVisible()
  // 数据态到位再断言：预览照常可读。
  await expect(dialog).toContainText('窗口版本')
  await expect
    .poll(() => visibleText(dialog.locator('.app-metric').nth(0)))
    .toBe('容量24')

  for (const name of ['落库此计划', '写入覆盖']) {
    const button = dialog.getByRole('button', { name })
    await expect(button).toBeDisabled()
    await expect(button).toHaveAttribute('title', READ_ONLY_REASON)
    await button.click({ force: true })
  }
  await expect(dialog.locator('.app-deny-note')).toHaveText(READ_ONLY_REASON)

  expect(apiRequests, 'a read-only showcase sends no schedule write').toEqual([])
})

test('invariant 8: the settings page shows the read-only reason and sends no password change', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/settings')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('账号设置')
  // 读操作照常：合成会话的邮箱可见。
  await expect(page.getByText('showcase@demo.local')).toBeVisible()

  const submit = page.getByRole('button', { name: '修改密码' })
  await expect(submit).toBeDisabled()
  await expect(submit).toHaveAttribute('title', READ_ONLY_REASON)
  await submit.click({ force: true })
  await expect(page.locator('.app-deny-note')).toHaveText(READ_ONLY_REASON)

  expect(apiRequests, 'a read-only showcase sends no password change').toEqual([])
})

test('invariant 8: the settings form ignores Enter while read-only', async ({ page }) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/settings')
  await page.getByLabel(/^当前密码/).fill('showcase-current')
  await page.getByLabel(/^新密码/).fill('showcase-next')
  await page.getByLabel(/^确认新密码/).fill('showcase-next')
  await page.getByLabel(/^确认新密码/).press('Enter')
  // Chromium 对「唯一提交按钮被禁用」的表单不派发隐式提交。
  // 用同一 submit 事件路径补一次，直接验证 onSubmit 的只读守卫。
  await page.locator('form').evaluate((form) => (form as HTMLFormElement).requestSubmit())

  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByRole('button', { name: '修改密码' })).toBeDisabled()
  await expect(page.locator('.app-deny-note')).toHaveText(READ_ONLY_REASON)
  expect(apiRequests, 'a read-only showcase sends no password change').toEqual([])
})

test('invariant 9: the showcase users page renders fixtures with zero requests', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/users')
  const table = page.getByRole('table', { name: '用户列表' })
  await expect(table).toBeVisible()
  // 夹具覆盖三个角色与两种状态。
  await expect(table).toContainText('master-one@example.com')
  await expect(table).toContainText('超级管理员')
  await expect(table).toContainText('member-one@example.com')
  await expect(table).toContainText('停用')

  // 读操作照常：筛选可用；单页夹具没有下一页。
  await expect(page.getByLabel('角色筛选')).toBeEnabled()
  await expect(page.getByLabel('邮箱前缀筛选')).toBeEnabled()
  await expect(page.getByRole('button', { name: '下一页' })).toBeDisabled()

  expect(apiRequests, 'the users fixture never touches the API').toEqual([])
})

test('invariant 10: the showcase drawer shows synthetic photos with zero requests', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/accounts')
  await page.getByRole('button', { name: /查看 138/ }).first().click()
  const drawer = page.getByRole('dialog', { name: '账号详情' })
  const strip = drawer.getByRole('region', { name: '账号照片' })
  await expect(strip).toBeVisible()
  await expect(strip.getByRole('img')).toHaveCount(3)
  await expect(drawer.getByRole('button', { name: '上传照片' })).toHaveCount(0)

  expect(apiRequests, 'the photo fixture never touches the API').toEqual([])
})

test('invariant 11: the showcase queue page renders fixtures with zero requests and disabled writes', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/queue')
  await expect(page.getByRole('heading', { level: 1, name: /今日\s*待办/ })).toBeVisible()
  await expect(page.locator('[data-queue-group="pending"]')).toContainText('138****8000')
  await expect(page.locator('[data-queue-group="failed"]')).toContainText('138****8003')
  await expect(page.locator('[data-queue-group="unsigned"]')).toContainText('138****8004')

  const approveBtn = page.getByRole('button', { name: '通过 138****8000' })
  await expect(approveBtn).toBeDisabled()
  await expect(approveBtn).toHaveAttribute('title', READ_ONLY_REASON)
  // 只读必须看得出来：禁用态要有视觉与指针差异，不能只看 disabled 属性
  expect(await approveBtn.evaluate((el) => getComputedStyle(el).cursor)).toBe('not-allowed')

  const retryBtn = page.getByRole('button', { name: '重试 138****8003' })
  await expect(retryBtn).toBeDisabled()
  await expect(retryBtn).toHaveAttribute('title', READ_ONLY_REASON)
  expect(await retryBtn.evaluate((el) => getComputedStyle(el).cursor)).toBe('not-allowed')

  // 详情抽屉在展示模式下仍可读合成诊断
  await page.getByRole('button', { name: '详情 138****8003' }).click()
  const drawer = page.getByRole('dialog', { name: '诊断 · 138****8003' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText('credential_invalid')

  expect(apiRequests, 'the showcase queue page never touches the API').toEqual([])
})

test('invariant 12: the showcase audit page renders fixtures with zero requests', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/audit')
  await expect(page.getByRole('heading', { level: 1, name: /审计\s*日志/ })).toBeVisible()
  await expect(page.locator('[data-audit-row]')).toHaveCount(8)
  await expect(page.locator('[data-audit-verdict]')).toContainText('链完好')
  await expect(page.getByText('auth.login', { exact: true })).toBeVisible()

  // 只读台账：筛选与详情仍可用，且都不发网络请求。
  await expect(page.getByLabel('动作筛选')).toBeEnabled()
  await page.getByRole('button', { name: '查看详情 #1042' }).click()
  const drawer = page.getByRole('dialog', { name: '审计详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText('"result":"ok"')

  expect(apiRequests, 'the audit fixture never touches the API').toEqual([])
})

test('invariant 13: the showcase logs page renders both tabs with zero requests', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/logs?tab=events')
  await expect(page.getByRole('heading', { level: 1, name: /签到\s*台账/ })).toBeVisible()
  await expect(page.getByRole('region', { name: '泳道 合成学校甲' })).toContainText('138****8001')

  await page.getByRole('tab', { name: '运行巡检' }).click()
  await expect(page.getByRole('heading', { name: '在飞清单' })).toBeVisible()
  await expect(page.locator('dl', { hasText: '待执行' })).toContainText('3')

  expect(apiRequests, 'the logs fixture never touches the API').toEqual([])
})

test('invariant 14: the showcase settings notify matrix renders fixtures with zero requests', async ({
  page,
}) => {
  const apiRequests = trackApiRequests(page)

  await page.goto('/settings')
  const card = page.locator('.card').filter({ hasText: '通知订阅' })
  await expect(card).toBeVisible()
  await expect(card).toContainText(READ_ONLY_REASON)

  const toggle = card.getByRole('switch', { name: '凭据失效 · 站内信' })
  await expect(toggle).toBeDisabled()
  await expect(toggle).toHaveAttribute('title', READ_ONLY_REASON)
  await toggle.click({ force: true })

  expect(apiRequests, 'a read-only showcase sends no admin notify request').toEqual([])
})
