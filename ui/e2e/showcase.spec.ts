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
 * 禁用按钮本身不派发 click；零请求断言是兜底，真正的判据是 disabled 与文案。
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

  const total = page.locator('.kpi-card').filter({ hasText: '任务总数' })
  await expect.poll(() => visibleText(total.locator('.kpi-value'))).toBe('4项')

  const card = page.locator('.card').filter({ hasText: '容量体检' })
  await expect(page.getByLabel('任务')).toHaveValue('school-a:task-daily')
  await expect
    .poll(() => visibleText(card.locator('.app-metric').filter({ hasText: '容量' })))
    .toBe('容量24')
  await expect
    .poll(() => visibleText(card.locator('.app-metric').filter({ hasText: '需求' })))
    .toBe('需求8')

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
  await expect(page.getByLabel('手机号筛选')).toBeEnabled()
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
