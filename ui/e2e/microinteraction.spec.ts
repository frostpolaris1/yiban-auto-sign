import { test, expect, type Locator, type Page } from '@playwright/test'
import { deleteCalls, mockAccountsApi } from './support/fake-accounts-api'
import { mockDashboardApi } from './support/fake-dashboard-api'
import {
  SCHEDULE_DAY,
  mockScheduleApi,
  overrideCalls,
  planCalls,
  timePrefPutCalls,
} from './support/fake-schedule-api'
import { createSchoolCalls, mockSchoolsApi } from './support/fake-schools-api'
import { visibleText } from './support/visible-text'

/**
 * 微交互批（3bw）的六条不变式。
 *
 * 1. 写操作成功 ⇒ 出现 toast（`data-sonner-toast`，成功类型），页面内不再有成功提示条；
 *    写操作失败 ⇒ 无 toast，错误文案留在模态或表单内。
 * 2. 时间偏好保存成功 ⇒ toast；429／403 ⇒ 无 toast，卡内错误文案。
 * 3. 看板 KPI、容量三格、预览三格的终值等于期望数字（不是「约等于」）。
 * 4. 列表过渡不回归：行增删后行点击仍开抽屉、菜单项不触发抽屉、焦点仍还原。
 * 5. 双主题：toast 在明暗两主题下都存在且可见。
 * 6. 展示模式：`e2e/showcase.spec.ts`。
 *
 * 断言用户可见结果，不钉内部调用顺序。数字读取穿透开放 shadow root，
 * 只算参与布局的字形（见 `support/visible-text.ts`）。
 */

function toast(page: Page): Locator {
  return page.locator('[data-sonner-toast]')
}

function kpiValue(page: Page, label: string): Locator {
  return page.locator('.kpi-card').filter({ hasText: label }).locator('.kpi-value')
}

function metric(page: Page, label: string): Locator {
  return page.locator('.app-metric').filter({ hasText: label })
}

async function openAccounts(page: Page) {
  await page.goto('/accounts')
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()
}

async function confirmPause(page: Page, phone: string) {
  await page.getByRole('button', { name: `暂停 ${phone}` }).click()
  await page.getByRole('dialog', { name: '暂停账号？' }).getByRole('button', { name: '确认暂停' }).click()
}

test('invariant 1: a successful account write toasts and leaves no in-page notice', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  await confirmPause(page, '139****9000')
  await expect(toast(page)).toHaveCount(1)
  await expect(toast(page)).toHaveAttribute('data-type', 'success')
  await expect(toast(page)).toContainText('已暂停 139****9000')
  await expect(page.getByText('操作结果')).toHaveCount(0)
  await expect(page.locator('.alert.success')).toHaveCount(0)

  await page.getByRole('button', { name: '签到 138****8000' }).click()
  await page.getByRole('dialog', { name: '立即签到？' }).getByRole('button', { name: '确认签到' }).click()
  await expect(toast(page).filter({ hasText: '已受理 138****8000 的签到任务' })).toBeVisible()

  await page.getByRole('button', { name: '恢复 135****0001' }).click()
  await page.getByRole('dialog', { name: '恢复账号？' }).getByRole('button', { name: '确认恢复' }).click()
  await expect(toast(page).filter({ hasText: '已恢复 135****0001' })).toBeVisible()
})

test('invariant 1: a review toasts only when the decision is accepted', async ({ page }) => {
  await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)

  await page.getByRole('button', { name: '评审 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '评审账号？' })
  await dialog.getByRole('button', { name: '提交评审' }).click()
  await expect(toast(page).filter({ hasText: '已完成 138****8000 的评审' })).toBeVisible()
  await expect(page.getByText('操作结果')).toHaveCount(0)
})

test('invariant 1: a failed sign keeps its error in the dialog and sends no toast', async ({
  page,
}) => {
  await mockAccountsApi(page, {
    role: 'user',
    signError: { status: 409, code: 'window_closed', message: '签到窗口已关闭' },
  })
  await openAccounts(page)

  await page.getByRole('button', { name: '签到 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '立即签到？' })
  await dialog.getByRole('button', { name: '确认签到' }).click()
  await expect(dialog).toContainText('签到窗口已关闭')
  await expect(dialog).toContainText('window_closed')
  await expect(toast(page)).toHaveCount(0)
})

test('invariant 1: a failed verify keeps its result in the dialog and sends no toast', async ({
  page,
}) => {
  await mockAccountsApi(page, {
    role: 'admin',
    verifyError: { status: 502, code: 'upstream_unavailable', message: '平台不可达' },
  })
  await openAccounts(page)

  await page.getByRole('button', { name: '验证 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '验证凭据？' })
  await dialog.getByRole('button', { name: '确认验证' }).click()
  await expect(dialog).toContainText('平台不可达')
  await expect(toast(page)).toHaveCount(0)
})

test('invariant 1: a platform rejection is a conclusion, not a success toast', async ({
  page,
}) => {
  await mockAccountsApi(page, {
    role: 'admin',
    verifyResult: { ok: false, code: 'credential_invalid', message: '凭据无效' },
  })
  await openAccounts(page)

  await page.getByRole('button', { name: '验证 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '验证凭据？' })
  await dialog.getByRole('button', { name: '确认验证' }).click()
  await expect(dialog).toContainText('验证失败：credential_invalid')
  await expect(toast(page)).toHaveCount(0)
})

test('invariant 1: a successful verify toasts and keeps the conclusion panel', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)

  await page.getByRole('button', { name: '验证 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '验证凭据？' })
  await dialog.getByRole('button', { name: '确认验证' }).click()
  await expect(toast(page).filter({ hasText: '已验证 138****8000' })).toBeVisible()
  await expect(dialog).toContainText('验证通过')
})

test('invariant 1: a created school toasts and appears without a page-level notice', async ({
  page,
}) => {
  const api = await mockSchoolsApi(page, { role: 'master' })
  await page.goto('/schools')
  await expect(page.getByRole('table', { name: '学校列表' })).toBeVisible()

  await page.getByRole('button', { name: '新建学校' }).click()
  const dialog = page.getByRole('dialog', { name: '新建学校？' })
  await dialog.getByLabel('学校代码').fill('syn-gamma')
  await dialog.getByLabel('学校名称').fill('合成学校丙')
  await dialog.getByRole('button', { name: '创建学校' }).click()

  await expect.poll(() => createSchoolCalls(api).length).toBe(1)
  await expect(toast(page).filter({ hasText: '已新建 合成学校丙' })).toBeVisible()
  await expect(page.getByText('操作结果')).toHaveCount(0)
})

test('invariant 1: a failed school create keeps its error in the modal and sends no toast', async ({
  page,
}) => {
  await mockSchoolsApi(page, {
    role: 'master',
    createError: { status: 409, code: 'conflict', message: '学校代码已存在' },
  })
  await page.goto('/schools')
  await page.getByRole('button', { name: '新建学校' }).click()
  const dialog = page.getByRole('dialog', { name: '新建学校？' })
  await dialog.getByLabel('学校代码').fill('syn-gamma')
  await dialog.getByLabel('学校名称').fill('合成学校丙')
  await dialog.getByRole('button', { name: '创建学校' }).click()

  await expect(dialog).toContainText('学校代码已存在')
  await expect(toast(page)).toHaveCount(0)
})

test('invariant 1: adding a school admin toasts inside the open modal', async ({ page }) => {
  await mockSchoolsApi(page, { role: 'master' })
  await page.goto('/schools')
  await page.getByRole('button', { name: '更多操作 合成学校甲' }).click()
  await page.getByRole('menu').getByRole('menuitem', { name: /管理员/ }).click()
  const dialog = page.getByRole('dialog', { name: '学校管理员' })
  await dialog.getByLabel('用户 ID').fill('user-9')
  await dialog.getByRole('button', { name: '添加管理员' }).click()

  await expect(toast(page).filter({ hasText: '已添加管理员 user-9' })).toBeVisible()
  await expect(page.getByText('操作结果')).toHaveCount(0)

  await dialog.getByRole('button', { name: '移除 user-9@example.com' }).click()
  await page
    .getByRole('dialog', { name: '移除管理员？' })
    .getByRole('button', { name: '确认移除' })
    .click()
  await expect(toast(page).filter({ hasText: '已移除管理员 user-9@example.com' })).toBeVisible()
})

test('invariant 1: a build toasts and a 409 build keeps its error in the confirm', async ({
  page,
}) => {
  const api = await mockScheduleApi(page, { role: 'master' })
  await page.goto(`/schedule?day=${SCHEDULE_DAY}`)
  await page.getByRole('button', { name: '查看计划 合成学校甲' }).click()
  const preview = page.getByRole('dialog', { name: '计划预览' })
  await preview.getByRole('button', { name: '落库此计划' }).click()
  await page.getByRole('dialog', { name: '落库此计划？' }).getByRole('button', { name: '确认落库' }).click()

  await expect.poll(() => planCalls(api).length).toBe(1)
  await expect(toast(page).filter({ hasText: `已落库 ${SCHEDULE_DAY} 的计划` })).toBeVisible()
  await expect(page.getByText('操作结果')).toHaveCount(0)
})

test('invariant 1: a 409 build leaves the error in the confirm and sends no toast', async ({
  page,
}) => {
  await mockScheduleApi(page, {
    role: 'master',
    planError: { status: 409, code: 'conflict', message: 'task disabled' },
  })
  await page.goto(`/schedule?day=${SCHEDULE_DAY}`)
  await page.getByRole('button', { name: '查看计划 合成学校甲' }).click()
  const preview = page.getByRole('dialog', { name: '计划预览' })
  await preview.getByRole('button', { name: '落库此计划' }).click()
  const confirm = page.getByRole('dialog', { name: '落库此计划？' })
  await confirm.getByRole('button', { name: '确认落库' }).click()

  await expect(confirm).toContainText('任务已停用')
  await expect(toast(page)).toHaveCount(0)
})

test('invariant 1: an override toasts the rebuilt version', async ({ page }) => {
  const api = await mockScheduleApi(page, { role: 'master' })
  await page.goto(`/schedule?day=${SCHEDULE_DAY}`)
  await page.getByRole('button', { name: '查看计划 合成学校甲' }).click()
  const preview = page.getByRole('dialog', { name: '计划预览' })
  await preview.getByRole('button', { name: '写入覆盖' }).click()
  const form = page.getByRole('dialog', { name: '写入窗口覆盖？' })
  await form.getByLabel('开始时间').fill('2026-10-28T08:30')
  await form.getByLabel('结束时间').fill('2026-10-28T09:20')
  await form.getByLabel('修改原因').fill('合成调整窗口')
  await form.getByRole('button', { name: '下一步：确认覆盖' }).click()
  await page.getByRole('dialog', { name: '写入覆盖？' }).getByRole('button', { name: '确认覆盖' }).click()

  await expect.poll(() => overrideCalls(api).length).toBe(1)
  await expect(toast(page).filter({ hasText: '已写入覆盖，计划已重建（v3）' })).toBeVisible()
  await expect(page.getByText('操作结果')).toHaveCount(0)
})

test('invariant 2: saving the time preference toasts', async ({ page }) => {
  const api = await mockScheduleApi(page, { role: 'user' })
  await page.goto('/')
  await page.getByLabel('偏移分钟').selectOption('20')
  await page.getByRole('button', { name: '保存偏好' }).click()
  await page.getByRole('dialog', { name: '保存时间偏好？' }).getByRole('button', { name: '确认保存' }).click()

  await expect.poll(() => timePrefPutCalls(api).length).toBe(1)
  await expect(toast(page).filter({ hasText: '已保存时间偏好：窗口开始 + 20 分钟' })).toBeVisible()
  await expect(page.getByText('操作结果')).toHaveCount(0)
})

for (const failure of [
  { status: 429, code: 'rate_limited', message: '限流', shown: '修改太频繁，请稍后再试' },
  { status: 403, code: 'forbidden', message: '无权', shown: '此部署未开放时间偏好' },
] as const) {
  test(`invariant 2: a ${failure.status} save keeps the error in the card and sends no toast`, async ({
    page,
  }) => {
    await mockScheduleApi(page, {
      role: 'user',
      timePrefPutError: { status: failure.status, code: failure.code, message: failure.message },
    })
    await page.goto('/')
    await page.getByLabel('偏移分钟').selectOption('20')
    await page.getByRole('button', { name: '保存偏好' }).click()
    const confirm = page.getByRole('dialog', { name: '保存时间偏好？' })
    await confirm.getByRole('button', { name: '确认保存' }).click()

    await expect(confirm).toContainText(failure.shown)
    await expect(confirm).toBeVisible()
    await expect(toast(page)).toHaveCount(0)
  })
}

test('invariant 3: dashboard KPI numbers settle on the exact counts', async ({ page }) => {
  const items = [
    { account_id: 'acc-0001', state: 'success', run_at: null, stale: false },
    { account_id: 'acc-0002', state: 'success', run_at: null, stale: false },
    { account_id: 'acc-0003', state: 'pending', run_at: null, stale: false },
    { account_id: 'acc-0004', state: 'running', run_at: null, stale: false },
    { account_id: 'acc-0005', state: 'failed', run_at: null, stale: false },
    { account_id: 'acc-0006', state: 'skipped', run_at: null, stale: false },
    { account_id: 'acc-0007', state: null, run_at: null, stale: false },
  ]
  await mockDashboardApi(page, { role: 'user', signStatusItems: items })
  await page.goto('/')
  await expect(page.getByRole('table', { name: '今日签到状态' })).toBeVisible()

  await expect.poll(() => visibleText(kpiValue(page, '账号总数'))).toBe('7个')
  await expect.poll(() => visibleText(kpiValue(page, '已签'))).toBe('2个')
  await expect.poll(() => visibleText(kpiValue(page, '进行中/待签'))).toBe('2个')
  await expect.poll(() => visibleText(kpiValue(page, '异常'))).toBe('2个')
})

test('invariant 3: capacity numbers settle on the exact report', async ({ page }) => {
  await mockDashboardApi(page, { role: 'admin' })
  await page.goto('/')
  await expect(page.getByLabel('任务')).toHaveValue('school-alpha:task-daily')

  await expect.poll(() => visibleText(metric(page, '容量'))).toBe('容量40')
  await expect.poll(() => visibleText(metric(page, '需求'))).toBe('需求12')
  await expect.poll(() => visibleText(metric(page, '缺口'))).toBe('缺口0')

  await page.getByLabel('任务').selectOption('school-beta:task-night')
  await expect.poll(() => visibleText(metric(page, '容量'))).toBe('容量20')
  await expect.poll(() => visibleText(metric(page, '需求'))).toBe('需求26')
  await expect.poll(() => visibleText(metric(page, '缺口'))).toBe('缺口6')
})

test('invariant 3: preview numbers settle on the exact plan', async ({ page }) => {
  await mockScheduleApi(page, { role: 'master' })
  await page.goto(`/schedule?day=${SCHEDULE_DAY}`)
  await page.getByRole('button', { name: '查看计划 合成学校甲' }).click()
  const dialog = page.getByRole('dialog', { name: '计划预览' })
  await expect(dialog.locator('.app-metric')).toHaveCount(3)

  await expect.poll(() => visibleText(dialog.locator('.app-metric').nth(0))).toBe('容量40')
  await expect.poll(() => visibleText(dialog.locator('.app-metric').nth(1))).toBe('需求12')
  await expect.poll(() => visibleText(dialog.locator('.app-metric').nth(2))).toBe('缺口0')

  // 预览里的指标格与看板同用 `.app-metric-value`：行盒高度同样不能变。
  // 轮询高度而非读一次：等弹窗渲染落定，不靠前面的数字轮询顺带等待。
  await expect
    .poll(async () => {
      const box = await dialog.locator('.app-metric-value').first().boundingBox()
      return box === null ? null : Math.round(box.height)
    })
    .toBe(24)
})

test('invariant 3: number components keep the line box of plain text', async ({ page }) => {
  // 行盒高度是布局契约，不是动画细节。44px 与 20px 分别代表 KPI 与指标。
  await mockDashboardApi(page, { role: 'admin' })
  await page.goto('/')
  await expect(page.getByLabel('任务')).toHaveValue('school-alpha:task-daily')

  const kpi = await page.locator('.kpi-card .kpi-value').first().boundingBox()
  const metric = await page.locator('.app-metric-value').first().boundingBox()
  expect(kpi, 'KPI 行盒已渲染').not.toBeNull()
  expect(metric, '指标行盒已渲染').not.toBeNull()
  expect(Math.round(kpi!.height), 'KPI 行盒').toBe(44)
  expect(Math.round(metric!.height), '指标行盒').toBe(24)

  // 徽章高度由内容撑开：负边距若落进来会把徽章压得比内容还小。
  const badge = page.locator('.app-metric .badge').first()
  const badgeBox = await badge.boundingBox()
  const hostBox = await badge.locator('.app-number').boundingBox()
  expect(badgeBox, '缺口徽章已渲染').not.toBeNull()
  expect(hostBox, '徽章内的数字已渲染').not.toBeNull()
  expect(badgeBox!.height, '徽章容纳其内容').toBeGreaterThan(hostBox!.height)
})

test('invariant 4: rows stay clickable and focus returns after a delete', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'user' })
  await openAccounts(page)

  await page.getByRole('button', { name: '更多操作 135****0001' }).click()
  await page.getByRole('menu').getByRole('menuitem', { name: /删除/ }).click()
  const dialog = page.getByRole('dialog', { name: '删除账号？' })
  await dialog.getByRole('button', { name: '确认删除' }).click()

  await expect.poll(() => deleteCalls(api, 'acc-3').length).toBe(1)
  await expect.poll(() => page.getByRole('button', { name: '查看 135****0001' }).count()).toBe(0)
  await expect(toast(page).filter({ hasText: '已删除 135****0001' })).toBeVisible()
  await expect
    .poll(() => page.evaluate(() => document.activeElement?.getAttribute('aria-label')))
    .toBe('手机号筛选')

  // 列表过渡后剩余行仍可点：点行开抽屉，按钮开抽屉，菜单项不开抽屉。
  await page.getByRole('cell', { name: 'school-b', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '账号详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText('139****9000')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  await page.getByRole('button', { name: '更多操作 139****9000' }).click()
  await page.getByRole('menu').getByRole('menuitem', { name: /编辑/ }).click()
  await expect(page.getByRole('dialog', { name: '编辑账号？' })).toBeVisible()
  await expect(drawer).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '编辑账号？' })).toBeHidden()
  await expect.poll(() => page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? null))
    .toMatch(/^更多操作 139\*\*\*\*9000$/)
})

test('invariant 4: a newly created school row opens the drawer', async ({ page }) => {
  await mockSchoolsApi(page, { role: 'master' })
  await page.goto('/schools')
  await page.getByRole('button', { name: '新建学校' }).click()
  const dialog = page.getByRole('dialog', { name: '新建学校？' })
  await dialog.getByLabel('学校代码').fill('syn-gamma')
  await dialog.getByLabel('学校名称').fill('合成学校丙')
  await dialog.getByRole('button', { name: '创建学校' }).click()

  await page.getByRole('button', { name: '查看 合成学校丙' }).click()
  const drawer = page.getByRole('dialog', { name: '学校详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText('合成学校丙')
})

test('invariant 4: schedule rows stay clickable after an override rebuild', async ({ page }) => {
  await mockScheduleApi(page, { role: 'master' })
  await page.goto(`/schedule?day=${SCHEDULE_DAY}`)
  await page.getByRole('button', { name: '查看计划 合成学校甲' }).click()
  const preview = page.getByRole('dialog', { name: '计划预览' })
  await preview.getByRole('button', { name: '写入覆盖' }).click()
  const form = page.getByRole('dialog', { name: '写入窗口覆盖？' })
  await form.getByLabel('开始时间').fill('2026-10-28T08:30')
  await form.getByLabel('结束时间').fill('2026-10-28T09:20')
  await form.getByLabel('修改原因').fill('合成调整窗口')
  await form.getByRole('button', { name: '下一步：确认覆盖' }).click()
  await page.getByRole('dialog', { name: '写入覆盖？' }).getByRole('button', { name: '确认覆盖' }).click()
  await expect(preview).toContainText('v3')
  await preview.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(preview).toBeHidden()

  await page.getByRole('button', { name: '查看计划 合成学校丙' }).click()
  await expect(page.getByRole('dialog', { name: '计划预览' })).toContainText('合成学校丙')
})

test.describe('motion enabled', () => {
  // 行为用例全局减少动效；这条用真实动效跑退场节点路径，确保过渡期间列表仍可用。
  test.use({ reducedMotion: 'no-preference' })

  test('invariant 4: a deleting row keeps the remaining rows clickable while it animates out', async ({
    page,
  }) => {
    const api = await mockAccountsApi(page, { role: 'user' })
    await openAccounts(page)

    await page.getByRole('button', { name: '更多操作 135****0001' }).click()
    await page.getByRole('menu').getByRole('menuitem', { name: /删除/ }).click()
    await page
      .getByRole('dialog', { name: '删除账号？' })
      .getByRole('button', { name: '确认删除' })
      .click()
    await expect.poll(() => deleteCalls(api, 'acc-3').length).toBe(1)

    // 不等退场结束：立即点另一行，抽屉要开。
    await page.getByRole('cell', { name: 'school-b', exact: true }).click()
    const drawer = page.getByRole('dialog', { name: '账号详情' })
    await expect(drawer).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(drawer).toBeHidden()
  })
})

for (const theme of ['light', 'dark'] as const) {
  test(`invariant 5: the toast is visible in ${theme}`, async ({ page }) => {
    await page.addInitScript((value) => {
      try {
        localStorage.setItem('dash26-theme', value)
      } catch {
        /* localStorage 禁用时主题退回媒体查询，下面的断言会明确失败。 */
      }
    }, theme)
    await mockAccountsApi(page, { role: 'master' })
    await openAccounts(page)
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme)

    await confirmPause(page, '139****9000')
    const success = toast(page).filter({ hasText: '已暂停 139****9000' })
    await expect(success).toBeVisible()
    await expect(success).toHaveAttribute('data-type', 'success')
    // 入场过渡把不透明度从 0 拉到 1；等它落定再判可读性。
    await expect
      .poll(() => success.evaluate((element) => getComputedStyle(element).opacity))
      .toBe('1')

    const paint = await success.evaluate((element) => {
      const style = getComputedStyle(element)
      return { color: style.color, background: style.backgroundColor }
    })
    expect(paint.color, `the toast text is painted in ${theme}`).not.toBe('rgba(0, 0, 0, 0)')
    expect(paint.background, `the toast has an opaque surface in ${theme}`).not.toBe(
      'rgba(0, 0, 0, 0)',
    )
  })
}
