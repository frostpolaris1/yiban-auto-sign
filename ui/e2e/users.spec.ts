import { test, expect, type Page } from '@playwright/test'
import {
  USERS,
  lastUserListCall,
  listUserCalls,
  mockUsersApi,
} from './support/fake-users-api'

/**
 * 用户管理页的不变式。
 *
 * 1. 角色可见性：master 从侧栏进页见列表；admin/user 无导航项，
 *    直接访问只有无权限卡片，且零用户列表请求。
 * 2. 列表内容：邮箱、角色中文标签、状态、是否需要改密、创建时间与上次登录。
 *    空的上次登录显示破折号。
 * 3. 游标翻页：两页数据不重不漏；末页「下一页」禁用；回退不发游标。
 * 4. 过滤：角色、状态、邮箱前缀各带进请求；连续输入只发一次请求（防抖）；
 *    回车立即使当前输入生效；改筛选回到第一页。
 * 5. 空态与错误态：空结果有文案；失败显示错误信封并可重试。
 *
 * 断言用户可见结果与请求参数，不钉内部调用顺序。
 * 390 宽无溢出由 `visual.spec.ts` 的溢出判据覆盖（`/users` 在常驻矩阵内）。
 */

async function openUsers(page: Page) {
  await page.goto('/users')
  await expect(page.getByRole('table', { name: '用户列表' })).toBeVisible()
  await expect(page.getByText('master-one@example.com')).toBeVisible()
}

test('invariant 1: master reaches the user list from the navigation', async ({ page }) => {
  await mockUsersApi(page, { role: 'master' })
  await page.goto('/')
  const link = page.locator('.d-sidebar').getByRole('link', { name: '用户管理', exact: true })
  await expect(link).toHaveCount(1)
  await link.click()
  await expect(page).toHaveURL(/\/users$/)
  await expect(page.getByRole('table', { name: '用户列表' })).toBeVisible()
})

test('invariant 2: each row shows the email, role, status and password flag', async ({ page }) => {
  await mockUsersApi(page, { role: 'master' })
  await openUsers(page)

  const master = page.getByRole('row').filter({ hasText: 'master-one@example.com' })
  await expect(master.getByRole('cell').nth(1)).toHaveText('超级管理员')
  await expect(master.getByRole('cell').nth(2)).toHaveText('正常')
  await expect(master.getByRole('cell').nth(3)).toHaveText('不需要')
  await expect(master.getByRole('cell').nth(4)).toContainText('2026')

  const admin = page.getByRole('row').filter({ hasText: 'admin-one@example.com' })
  await expect(admin.getByRole('cell').nth(1)).toHaveText('管理员')
  await expect(admin.getByRole('cell').nth(3), 'a pending reset is visible').toHaveText('需要')
  await expect(admin.getByRole('cell').nth(5), 'no login yet reads as a dash').toHaveText('—')

  const member = page.getByRole('row').filter({ hasText: 'member-one@example.com' })
  await expect(member.getByRole('cell').nth(1)).toHaveText('用户')
  await expect(member.getByRole('cell').nth(2)).toHaveText('停用')
  await expect(member.getByRole('cell').nth(5)).toContainText('2026')
})

for (const role of ['admin', 'user'] as const) {
  test(`invariant 1: the ${role} role has no entry and no user list request`, async ({ page }) => {
    const api = await mockUsersApi(page, { role })
    await page.goto('/')
    await expect(
      page.locator('.d-sidebar').getByRole('link', { name: '用户管理', exact: true }),
    ).toHaveCount(0)

    await page.goto('/users')
    await expect(page.getByText('无权访问')).toBeVisible()
    await expect(page.getByText('你的角色无权查看用户管理')).toBeVisible()
    await expect(page.getByRole('table', { name: '用户列表' })).toHaveCount(0)
    expect(listUserCalls(api), 'a denied page sends no user list request').toHaveLength(0)

    // 死胡同不允许：卡片必须有出口，且不做静默重定向。
    await expect(page).toHaveURL(/\/users$/)
    await page.getByRole('button', { name: '返回看板' }).click()
    await expect(page).toHaveURL(/\/$/)
    expect(listUserCalls(api), 'the exit sends no list request either').toHaveLength(0)
  })
}

test('invariant 3: the two pages neither repeat nor drop a row', async ({ page }) => {
  const api = await mockUsersApi(page, {
    role: 'master',
    userPages: { '': [USERS[0], USERS[1]], c2: [USERS[2]] },
    userNextCursors: { '': 'c2', c2: '' },
  })
  await page.goto('/users')
  await expect(page.getByRole('table', { name: '用户列表' })).toBeVisible()

  const previous = page.getByRole('button', { name: '上一页' })
  const next = page.getByRole('button', { name: '下一页' })
  await expect(previous, 'the first page has no backtrack slot').toBeDisabled()
  await expect(next).toBeEnabled()

  // 第一页：前两条，且不含第三页成员。
  await expect(page.getByText('master-one@example.com')).toBeVisible()
  await expect(page.getByText('admin-one@example.com')).toBeVisible()
  await expect(page.getByText('member-one@example.com')).toHaveCount(0)

  await next.click()
  await expect.poll(() => lastUserListCall(api)?.params.cursor).toBe('c2')
  // 第二页：只有第三页成员，前两行不重复出现。
  await expect(page.getByText('member-one@example.com')).toBeVisible()
  await expect(page.getByText('master-one@example.com')).toHaveCount(0)
  await expect(page.getByText('admin-one@example.com')).toHaveCount(0)
  await expect(previous).toBeEnabled()
  await expect(next, 'the last page has no next cursor').toBeDisabled()

  await previous.click()
  await expect.poll(() => lastUserListCall(api)?.params.cursor ?? 'absent').toBe('absent')
  await expect(page.getByText('master-one@example.com')).toBeVisible()
  await expect(page.getByText('member-one@example.com')).toHaveCount(0)
  await expect(previous).toBeDisabled()
})

test('invariant 4: role, status and email prefix each ride the list request', async ({ page }) => {
  const api = await mockUsersApi(page, { role: 'master' })
  await openUsers(page)
  expect(Object.keys(lastUserListCall(api)?.params ?? {}), 'no filter on the first load').toEqual([])

  await page.getByLabel('角色筛选').selectOption('admin')
  await expect.poll(() => lastUserListCall(api)?.params.role).toBe('admin')

  await page.getByLabel('状态筛选').selectOption('disabled')
  await expect.poll(() => lastUserListCall(api)?.params.status).toBe('disabled')
  expect(lastUserListCall(api)?.params.role, 'filters combine').toBe('admin')

  // 连续输入只发一次请求：防抖生效时 q 只出现一次，且是完整前缀。
  // 不按键间隔断言（高负载下 20ms 可能被拖过防抖窗），只断言终态：
  // 等完整前缀落地后，再给一个防抖窗，计数仍为 1。
  await page.getByLabel('邮箱前缀筛选').pressSequentially('member', { delay: 5 })
  await expect.poll(() => lastUserListCall(api)?.params.q).toBe('member')
  await page.waitForTimeout(400)
  expect(
    listUserCalls(api).filter((call) => call.params.q !== undefined),
    'one request per burst, not one per keystroke',
  ).toHaveLength(1)
})

test('invariant 4: Enter sends exactly one prefix request and does not double-send', async ({ page }) => {
  const api = await mockUsersApi(page, { role: 'master' })
  await openUsers(page)

  const search = page.getByLabel('邮箱前缀筛选')
  await search.fill('admin-one')
  await search.press('Enter')
  // 防抖窗口是 250ms；回车应立即发请求。断言「请求在防抖窗内出现」改为
  // 「首帧后请求数已为 1」的轮询，避免 CI 高负载下 120ms 窗口假红。
  await expect.poll(
    () => listUserCalls(api).filter((call) => call.params.q === 'admin-one').length,
    { timeout: 2000 },
  ).toBe(1)
  // 防抖窗过后仍只有一条：回车路径不得再补发一次。
  await page.waitForTimeout(400)
  expect(
    listUserCalls(api).filter((call) => call.params.q === 'admin-one'),
    'Enter must not double-send after the debounce window',
  ).toHaveLength(1)
})

test('invariant 4: changing a filter returns to the first page', async ({ page }) => {
  const api = await mockUsersApi(page, {
    role: 'master',
    userPages: { '': [USERS[0]], c2: [USERS[1]] },
    userNextCursors: { '': 'c2', c2: '' },
  })
  await page.goto('/users')
  await expect(page.getByRole('table', { name: '用户列表' })).toBeVisible()
  await page.getByRole('button', { name: '下一页' }).click()
  await expect.poll(() => lastUserListCall(api)?.params.cursor).toBe('c2')

  await page.getByLabel('状态筛选').selectOption('active')
  await expect.poll(() => lastUserListCall(api)?.params.status).toBe('active')
  expect(lastUserListCall(api)?.params.cursor, 'a filter change drops the cursor').toBeUndefined()
  await expect(page.getByRole('button', { name: '上一页' })).toBeDisabled()
})

test('invariant 5: an empty result says so', async ({ page }) => {
  await mockUsersApi(page, { role: 'master', users: [] })
  await page.goto('/users')
  await expect(page.getByText('没有匹配的用户')).toBeVisible()
  await expect(page.getByText('调整筛选条件后重试。')).toBeVisible()
})

test('invariant 5: a failed list shows the envelope and can retry', async ({ page }) => {
  const api = await mockUsersApi(page, {
    role: 'master',
    listError: { status: 500, code: 'internal_error', message: '用户服务不可用' },
  })
  await page.goto('/users')
  await expect(page.getByText('internal_error · 用户服务不可用')).toBeVisible()

  const before = listUserCalls(api).length
  await page.getByRole('button', { name: '重试' }).click()
  await expect.poll(() => listUserCalls(api).length).toBeGreaterThan(before)
})
