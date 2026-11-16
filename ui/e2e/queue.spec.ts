import { test, expect, type Page } from '@playwright/test'
import {
  QUEUE_ACCOUNTS,
  QUEUE_TASKS,
  accountListCalls,
  mockQueueApi,
  reviewCalls,
  signCalls,
  taskListCalls,
} from './support/fake-queue-api'
import { fakeSourceEmit, fakeSourceFail, fakeSourceOpen, installFakeEventSource } from './support/fake-run-api'
import { visibleText } from './support/visible-text'

/**
 * 待办页（/queue）的行为不变式：
 *
 * 1. 权限隔离：master/admin 经侧栏「待办」进入并正常加载；user 无侧栏入口，
 *    直接访问显示无权限卡片且零业务请求。
 * 2. 独立分段与独立游标：翻「今日失败」下一页不会改变「待审核」请求的 cursor；
 *    组内分段切换（待审核/已拒绝、待签/进行中）独立运作。
 * 3. 一键通过：review 恰好 1 次，该行离开待审组，摘要待审核计数 −1，出现 toast。
 * 4. 驳回理由弹窗：须填理由才能提交；提交后触发 review(reject)，账号转入「已拒绝」分段。
 * 5. 一键重签（重试）：sign 恰好 1 次；失败组保持该行（异步不乐观删行），出现 toast；
 *    失败时错误留在行内。
 * 6. 负向（全空与接口报错）：三组全空时分别呈现组专属空态，无整页空态；读失败出错误与重试。
 * 7. 诊断抽屉与 390px 无横向溢出。
 * 8. SSE 实时合并：突发多次 task.progress 只触发一次任务组重取，不连带重取学校列表。
 */

async function openQueue(page: Page) {
  await expect(page.getByRole('heading', { level: 1, name: /今日\s*待办/ })).toBeVisible()
  await expect(page.locator('[data-queue-group="pending"]')).toBeVisible()
  await expect(page.locator('[data-queue-group="failed"]')).toBeVisible()
  await expect(page.locator('[data-queue-group="unsigned"]')).toBeVisible()
}

test('invariant 1: master reaches /queue via sidebar; user is denied with zero requests', async ({
  page,
}) => {
  await installFakeEventSource(page)
  await mockQueueApi(page, { role: 'master' })
  await page.goto('/')
  const link = page.locator('.d-sidebar').getByRole('link', { name: '待办', exact: true })
  await expect(link).toHaveCount(1)
  await link.click()
  await expect(page).toHaveURL(/\/queue$/)
  await openQueue(page)

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  const userApi = await mockQueueApi(page, { role: 'user' })
  await page.goto('/')
  await expect(page.locator('.d-sidebar').getByRole('link', { name: '待办', exact: true })).toHaveCount(0)

  await page.goto('/queue')
  await expect(page.getByText('无权访问')).toBeVisible()
  await expect(page.getByText('你的角色无权查看待办队列')).toBeVisible()
  expect(accountListCalls(userApi)).toHaveLength(0)
  expect(taskListCalls(userApi)).toHaveLength(0)
})

test('invariant 2: group pagination cursors are completely independent across groups', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockQueueApi(page, {
    role: 'master',
    accountPages: {
      'pending:': [QUEUE_ACCOUNTS[0]],
      'pending:acc-c2': [QUEUE_ACCOUNTS[1]],
      'rejected:': [QUEUE_ACCOUNTS[2]],
    },
    accountNextCursors: {
      'pending:': 'acc-c2',
    },
    taskPages: {
      'failed:': [QUEUE_TASKS[0]],
      'failed:fail-c2': [
        {
          ...QUEUE_TASKS[0],
          id: 'q-task-fail-2',
          account_phone_masked: '138****8099',
          result: 'timeout',
        },
      ],
      'pending:': [QUEUE_TASKS[1]],
      'running:': [QUEUE_TASKS[2]],
    },
    taskNextCursors: {
      'failed:': 'fail-c2',
    },
  })

  await page.goto('/queue')
  await openQueue(page)

  const failedGroup = page.locator('[data-queue-group="failed"]')
  const pendingGroup = page.locator('[data-queue-group="pending"]')
  const unsignedGroup = page.locator('[data-queue-group="unsigned"]')

  await expect(failedGroup).toContainText('138****8003')
  const beforeAccountCalls = accountListCalls(api).length

  // 翻「今日失败」下一页
  await failedGroup.getByRole('button', { name: '下一页' }).click()
  await expect(failedGroup).toContainText('138****8099')

  // 「待审核」没有因为「今日失败」翻页而被重取或串游标
  expect(accountListCalls(api).length).toBe(beforeAccountCalls)
  const failedCalls = taskListCalls(api).filter((c) => c.params.state === 'failed')
  expect(failedCalls.map((c) => c.params.cursor ?? '')).toEqual(['', 'fail-c2'])

  // 切回「今日失败」上一页
  await failedGroup.getByRole('button', { name: '上一页' }).click()
  await expect(failedGroup).toContainText('138****8003')

  // 组内分段切换：待审核 -> 已拒绝；待签 -> 进行中
  await pendingGroup.getByRole('button', { name: /已拒绝/ }).click()
  await expect(pendingGroup).toContainText('135****0001')
  await expect(pendingGroup).toContainText('所属学校不匹配')

  await unsignedGroup.getByRole('button', { name: '进行中' }).click()
  await expect(unsignedGroup).toContainText('138****8001')
})

test('invariant 3 & 4: approve removes row & decrements summary; reject opens modal and moves row', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockQueueApi(page, { role: 'master' })
  await page.goto('/queue')
  await openQueue(page)

  const summary = page.locator('[data-queue-summary]')
  await expect
    .poll(() => visibleText(summary.locator('button').first().locator('.q-tele-value')))
    .toBe('3')

  const pendingGroup = page.locator('[data-queue-group="pending"]')
  await expect(pendingGroup).toContainText('138****8000')
  await expect(pendingGroup).toContainText('139****9000')

  // 一键通过 138****8000
  await pendingGroup.getByRole('button', { name: '通过 138****8000' }).click()
  expect(reviewCalls(api)).toHaveLength(1)
  expect(reviewCalls(api)[0].body).toEqual({ decision: 'approve' })

  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '已通过 东区宿舍' })).toBeVisible()
  await expect(pendingGroup.getByText('138****8000')).toHaveCount(0)
  await expect
    .poll(() => visibleText(summary.locator('button').first().locator('.q-tele-value')))
    .toBe('2')

  // 驳回 139****9000
  await pendingGroup.getByRole('button', { name: '驳回 139****9000' }).click()
  const modal = page.getByRole('dialog', { name: '驳回账号' })
  await expect(modal).toBeVisible()
  const confirmReject = modal.getByRole('button', { name: '确认驳回' })
  await expect(confirmReject).toBeDisabled()

  await modal.getByRole('button', { name: '所属学校不匹配' }).click()
  await expect(confirmReject).toBeEnabled()
  await confirmReject.click()

  await expect(modal).toBeHidden()
  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '已驳回 南区教学楼' })).toBeVisible()
  await expect(pendingGroup.getByText('139****9000')).toHaveCount(0)

  // 切换到已拒绝分段可看到刚驳回的账号
  await pendingGroup.getByRole('button', { name: /已拒绝/ }).click()
  await expect(pendingGroup).toContainText('139****9000')
})

test('invariant 5: retry dispatches sign once, keeps failed row, and shows inline error on failure', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockQueueApi(page, { role: 'master' })
  await page.goto('/queue')
  await openQueue(page)

  const failedGroup = page.locator('[data-queue-group="failed"]')
  await failedGroup.getByRole('button', { name: '重试 138****8003' }).click()

  expect(signCalls(api)).toHaveLength(1)
  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '已触发重签 138****8003' })).toBeVisible()
  // 重试是异步动作，失败行仍留在列表里
  await expect(failedGroup).toContainText('138****8003')

  // 失败场景：错误留在行内，不弹 toast
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await mockQueueApi(page, {
    role: 'master',
    signError: { status: 409, code: 'window_closed', message: '签到窗口已关闭' },
  })
  await page.goto('/queue')
  await openQueue(page)

  await failedGroup.getByRole('button', { name: '重试 138****8003' }).click()
  await expect(failedGroup.getByRole('alert')).toContainText('签到窗口已关闭')
})

test('invariant 6: all-empty groups show per-group empty copy; read errors render retryable alert', async ({
  page,
}) => {
  await installFakeEventSource(page)
  await mockQueueApi(page, { role: 'master', accounts: [], tasks: [] })
  await page.goto('/queue')
  await openQueue(page)

  await expect(page.getByText('待审核已清空')).toBeVisible()
  await expect(page.getByText('今日暂无失败').first()).toBeVisible()
  await expect(page.getByText('没有待签任务')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await mockQueueApi(page, {
    role: 'master',
    tasksError: { status: 500, code: 'internal_error', message: '任务服务暂不可用' },
  })
  await page.goto('/queue')
  await openQueue(page)

  const failedGroup = page.locator('[data-queue-group="failed"]')
  await expect(failedGroup.getByRole('alert')).toContainText('任务服务暂不可用')
  await expect(failedGroup.getByRole('button', { name: '重试' })).toBeVisible()
})

test('invariant 7: diagnostics drawer opens and closes, and 390px layout has zero horizontal overflow', async ({
  page,
}) => {
  await installFakeEventSource(page)
  await mockQueueApi(page, { role: 'master' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/queue')
  await openQueue(page)

  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1)

  const failedGroup = page.locator('[data-queue-group="failed"]')
  await failedGroup.getByRole('button', { name: '详情 138****8003' }).click()
  const drawer = page.getByRole('dialog', { name: '诊断 · 138****8003' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText('credential_invalid')
  await expect(drawer).toContainText('https://eg-1')

  await drawer.getByRole('button', { name: '关闭详情' }).click()
  await expect(drawer).toBeHidden()
})

test('invariant 8: SSE bursts coalesce into a single task refresh without refetching schools', async ({
  page,
}) => {
  await installFakeEventSource(page)
  const api = await mockQueueApi(page, { role: 'master' })
  await page.goto('/queue')
  await openQueue(page)

  await fakeSourceOpen(page)
  const initialTasks = taskListCalls(api).length
  const initialSchools = api.calls.filter((c) => c.path === '/api/schools').length
  const initialAccounts = accountListCalls(api).length

  // 短时间内连发 3 次任务事件，合并窗口后只重取 1 轮任务（failed + unsigned = 2 次），不重取学校与待审核账号
  await fakeSourceEmit(page, { type: 'task.progress', id: 'ev-1' })
  await fakeSourceEmit(page, { type: 'task.progress', id: 'ev-2' })
  await fakeSourceEmit(page, { type: 'task.concluded', id: 'ev-3' })

  await expect
    .poll(() => taskListCalls(api).length)
    .toBe(initialTasks + 2)
  expect(api.calls.filter((c) => c.path === '/api/schools').length).toBe(initialSchools)
  expect(accountListCalls(api).length).toBe(initialAccounts)

  // 断线出横幅并可重连
  await fakeSourceFail(page)
  await expect(page.getByText(/实时同步已断开/)).toBeVisible()
  await page.getByRole('button', { name: '重连' }).click()
  await expect(page.locator('.q-live-pill')).toContainText('实时')
})
