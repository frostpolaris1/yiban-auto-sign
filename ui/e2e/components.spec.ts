import { test, expect, type Page } from '@playwright/test'
import { mockAccountsApi } from './support/fake-accounts-api'

/**
 * UX-0 共用件（AsyncState、BusyButton、FilterField）的用户可见行为。
 *
 * 模拟的真实用户动作：打开一个列表卡（加载 → 失败 → 重试 → 数据），
 * 点一次「保存」并等它落定，向筛选框提交空关键字收到校验错误再输入。
 * 断言用户可见结果：错误是 alert 不是「暂无」，在途按钮禁用且标记 busy，
 * 标签点击聚焦控件、描述随提示/错误切换。
 *
 * 不钉组件名，不钉调用顺序。演示壳在 /design 画廊，非业务页。
 * 在途按钮的可及名会从「保存设置」变到「保存中…」，两个名字各配定位器。
 */

test.beforeEach(async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })
  await page.goto('/design')
})

/** 共用件演示卡：三件演示都在这张卡里。 */
function sharedCard(page: Page) {
  return page.locator('.card').filter({ hasText: '共用件' })
}

test('async state keeps error and empty apart, and retry reaches data', async ({ page }) => {
  const card = sharedCard(page)

  // 加载态：静默状态区，不是错误也不是空。
  await expect(card.getByRole('status').filter({ hasText: '正在读取运行日志' })).toBeVisible()

  // 错误态：alert 说失败原因；「暂无」不得出现，重试在场。
  await card.getByRole('button', { name: '错误', exact: true }).click()
  const alert = card.getByRole('alert')
  await expect(alert).toContainText('读取运行日志失败')
  await expect(alert, '错误不得渲染成空态文案').not.toContainText('暂无')
  await expect(card.getByText('暂无运行日志')).toHaveCount(0)
  await expect(card.getByRole('button', { name: '重试' })).toBeVisible()

  // 重试：回到数据态，alert 退场。
  await card.getByRole('button', { name: '重试' }).click()
  await expect(card.getByText('运行日志 3 条')).toBeVisible()
  await expect(card.getByRole('alert')).toHaveCount(0)

  // 空态：status 说「暂无」，没有 alert，也没有重试入口。
  await card.getByRole('button', { name: '空', exact: true }).click()
  await expect(card.getByRole('status').filter({ hasText: '今日暂无运行日志' })).toBeVisible()
  await expect(card.getByRole('alert')).toHaveCount(0)
  await expect(card.getByRole('button', { name: '重试' })).toHaveCount(0)
})

test('busy button blocks duplicate submits while in flight', async ({ page }) => {
  const card = sharedCard(page)
  const idle = card.getByRole('button', { name: '保存设置' })
  const inFlight = card.getByRole('button', { name: '保存中…' })

  await idle.click()

  // 在途：禁用、标记 busy、文案切换。禁用是浏览器的重复提交防线。
  await expect(inFlight).toBeDisabled()
  await expect(inFlight).toHaveAttribute('aria-busy', 'true')

  // 在途期间真用户再点也无效：强制点击走真实输入管线，仍被禁用挡下。
  await inFlight.click({ force: true, timeout: 2_000 })

  // 落定：恢复可点，且全程只发生一次提交。
  await expect(idle).toBeEnabled({ timeout: 5_000 })
  await expect(idle, '落定后不得再标 busy').not.toHaveAttribute('aria-busy', 'true')
  await expect(card.getByText('已保存 1 次'), '在途点击不得变成第二次提交').toBeVisible()
})

test('filter field keeps label, hint and error associated for screen readers', async ({ page }) => {
  const card = sharedCard(page)

  // 标签关联：getByLabel 能定位控件，点标签文字能聚焦控件。
  const input = page.getByLabel('关键字')
  await input.fill('合成学校甲')
  await expect(input).toHaveValue('合成学校甲')
  await card.locator('label').filter({ hasText: '关键字' }).click()
  await expect(input, '点标签应聚焦控件').toBeFocused()

  // 提示关联：aria-describedby 指向可见的提示；此时没有错误。
  const hintId = await input.getAttribute('aria-describedby')
  expect(hintId, '无错误时描述指向提示').toBeTruthy()
  const hint = card.locator(`[id="${hintId}"]`)
  await expect(hint).toContainText('按学校名或编号筛选')
  await expect(card.getByRole('alert')).toHaveCount(0)
  expect(await input.getAttribute('aria-invalid'), '无错误不得标 aria-invalid').toBeNull()

  // 清空后提交：错误可见、读屏可感知（role=alert），描述切换到错误。
  await input.fill('')
  await card.getByRole('button', { name: '提交筛选' }).click()
  const errorId = await input.getAttribute('aria-describedby')
  expect(errorId, '有错误时描述指向错误').toBeTruthy()
  expect(errorId, '描述必须随错误切换').not.toBe(hintId)
  const error = card.locator(`[id="${errorId}"]`)
  await expect(error).toHaveAttribute('role', 'alert')
  await expect(error).toContainText('关键字不能为空')
  await expect(input).toHaveAttribute('aria-invalid', 'true')
  await expect(card.getByText('按学校名或编号筛选')).toHaveCount(0)

  // 重新输入：错误退场，描述回到提示。
  await input.fill('合成学校乙')
  await expect(card.getByRole('alert')).toHaveCount(0)
  const hintIdAfter = await input.getAttribute('aria-describedby')
  expect(hintIdAfter, '错误清掉后描述回到提示').toBe(hintId)
  expect(await input.getAttribute('aria-invalid')).toBeNull()
})
