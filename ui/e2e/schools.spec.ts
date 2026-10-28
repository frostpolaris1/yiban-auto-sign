import { test, expect, type Page } from '@playwright/test'
import {
  ADMINS,
  SCHOOLS,
  adminAddCalls,
  adminListCalls,
  adminRemoveCalls,
  createSchoolCalls,
  lastSchoolListCall,
  listSchoolCalls,
  mockSchoolsApi,
  patchSchoolCalls,
} from './support/fake-schools-api'

/**
 * 学校页的六条不变式。
 *
 * 1. 角色可见性：master/admin 进页见列表；user 无学校导航项，
 *    直接访问只有无权限卡片，且零学校列表请求。
 * 2. 新建：master 可用；admin/user 置灰并给原因。code 非法被前端拒绝，
 *    合法后 POST 体只含契约字段。
 * 3. 编辑：仅 master；表单预填；只发变更字段；无变更保存禁用。
 * 4. 管理员：仅 master；打开即拉全部游标页；添加需要 user_id；
 *    移除必经确认模态；成功后列表刷新。
 * 5. 分页：游标栈只回退已访问页；无回退位禁用；空态有文案。
 * 6. 抽屉与三个模态：焦点进入、Esc 与遮罩关闭、焦点还原。
 *
 * 断言用户可见结果，不钉内部调用顺序。
 */

async function focusIsInside(page: Page, selector: string): Promise<boolean> {
  return page.evaluate((value) => {
    const active = document.activeElement
    const target = document.querySelector(value)
    return Boolean(active && target && target.contains(active))
  }, selector)
}

async function activeLabel(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? null)
}

async function activeText(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.textContent?.trim() ?? null)
}

async function openSchools(page: Page) {
  await page.goto('/schools')
  await expect(page.getByRole('table', { name: '学校列表' })).toBeVisible()
  await expect(page.getByRole('button', { name: '查看 合成学校甲' })).toBeVisible()
}

async function openRowMenu(page: Page, name: string) {
  await page.getByRole('button', { name: `更多操作 ${name}` }).click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  return menu
}

for (const role of ['master', 'admin'] as const) {
  test(`invariant 1: ${role} reaches the school list from the navigation`, async ({ page }) => {
    await mockSchoolsApi(page, { role })
    await page.goto('/')
    const link = page.locator('.d-sidebar').getByRole('link', { name: '学校', exact: true })
    await expect(link).toHaveCount(1)
    await link.click()
    await expect(page).toHaveURL(/\/schools$/)
    await expect(page.getByRole('table', { name: '学校列表' })).toBeVisible()
  })
}

test('invariant 1: a user has no school entry and no list request', async ({ page }) => {
  const api = await mockSchoolsApi(page, { role: 'user' })
  await page.goto('/')
  await expect(
    page.locator('.d-sidebar').getByRole('link', { name: '学校', exact: true }),
  ).toHaveCount(0)

  await page.goto('/schools')
  await expect(page.getByText('无权访问')).toBeVisible()
  await expect(page.getByText('你的角色无权查看学校管理')).toBeVisible()
  await expect(page.getByRole('table', { name: '学校列表' })).toHaveCount(0)
  expect(listSchoolCalls(api), 'a denied page sends no list request').toHaveLength(0)

  // 死胡同不允许：卡片必须有出口，且不做静默重定向。
  await expect(page).toHaveURL(/\/schools$/)
  await expect(page.getByRole('button', { name: '返回上一页' })).toHaveCount(0)
  await page.getByRole('button', { name: '返回看板' }).click()
  await expect(page).toHaveURL(/\/$/)
  expect(listSchoolCalls(api), 'the exit sends no list request either').toHaveLength(0)

  await page.goto('/schools')
  await expect(page.getByRole('button', { name: '新建学校' })).toBeDisabled()
  await expect(page.getByText('仅 master 可新建学校')).toBeVisible()
})

test('invariant 1: the denial card offers the previous page when the visit has a source', async ({
  page,
}) => {
  await mockSchoolsApi(page, { role: 'user' })
  await page.goto('/schools', { referer: 'http://127.0.0.1:4173/' })
  await expect(page.getByText('无权访问')).toBeVisible()
  await expect(page.getByRole('button', { name: '返回上一页' })).toBeVisible()
})

test('the page has no filter bar because the contract has no parameters', async ({ page }) => {
  const api = await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)
  await expect(page.getByRole('combobox')).toHaveCount(0)
  expect(Object.keys(lastSchoolListCall(api)?.params ?? {}), 'no filter rides the list call').toEqual(
    [],
  )
})

test('invariant 2: only master may create and the code pattern is enforced', async ({ page }) => {
  const api = await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const create = page.getByRole('button', { name: '新建学校' })
  await expect(create).toBeEnabled()
  await create.click()

  const dialog = page.getByRole('dialog', { name: '新建学校？' })
  await expect(dialog).toBeVisible()
  const save = dialog.getByRole('button', { name: '创建学校' })
  await expect(save).toBeDisabled()

  const code = dialog.getByLabel('学校代码')
  await code.fill('Abc')
  await expect(save).toBeDisabled()
  await expect(dialog).toContainText('小写')
  await code.fill('a_b')
  await expect(save).toBeDisabled()
  await code.fill('a')
  await expect(save).toBeDisabled()
  await code.fill('a'.repeat(33))
  await expect(save).toBeDisabled()

  await dialog.getByLabel('学校名称').fill('合成学校丙')
  await expect(save, 'an invalid code keeps save off').toBeDisabled()
  await code.fill('syn-gamma')
  await expect(save).toBeEnabled()

  await save.click()
  await expect.poll(() => createSchoolCalls(api).length).toBe(1)
  expect(createSchoolCalls(api)[0].body).toEqual({
    code: 'syn-gamma',
    name: '合成学校丙',
    timezone: 'Asia/Shanghai',
  })
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('button', { name: '查看 合成学校丙' })).toBeVisible()
})

test('invariant 2: an admin sees the create button greyed with its reason', async ({ page }) => {
  const api = await mockSchoolsApi(page, { role: 'admin' })
  await openSchools(page)

  await expect(page.getByRole('button', { name: '新建学校' })).toBeDisabled()
  await expect(page.getByText('仅 master 可新建学校')).toBeVisible()
  expect(createSchoolCalls(api)).toHaveLength(0)
})

test('invariant 3: edit prefills, sends only the changed field and blocks a no-op', async ({
  page,
}) => {
  const api = await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const menu = await openRowMenu(page, '合成学校甲')
  await menu.getByRole('menuitem', { name: /编辑/ }).click()
  const dialog = page.getByRole('dialog', { name: '编辑学校？' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('syn-alpha')
  await expect(dialog.getByLabel('学校名称')).toHaveValue('合成学校甲')
  await expect(dialog.getByLabel('状态')).toHaveValue('active')
  await expect(dialog.getByLabel('时区')).toHaveValue('Asia/Shanghai')

  const save = dialog.getByRole('button', { name: '保存修改' })
  await expect(save, 'an unchanged form stays closed').toBeDisabled()
  expect(patchSchoolCalls(api, 'school-alpha')).toHaveLength(0)

  await dialog.getByLabel('学校名称').fill('合成学校甲二')
  await expect(save).toBeEnabled()
  await save.click()
  await expect.poll(() => patchSchoolCalls(api, 'school-alpha').length).toBe(1)
  expect(patchSchoolCalls(api, 'school-alpha')[0].body).toEqual({ name: '合成学校甲二' })
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('button', { name: '查看 合成学校甲二' })).toBeVisible()

  const menu2 = await openRowMenu(page, '合成学校甲二')
  await menu2.getByRole('menuitem', { name: /编辑/ }).click()
  const dialog2 = page.getByRole('dialog', { name: '编辑学校？' })
  await dialog2.getByLabel('状态').selectOption('disabled')
  await dialog2.getByRole('button', { name: '保存修改' }).click()
  await expect.poll(() => patchSchoolCalls(api, 'school-alpha').length).toBe(2)
  expect(patchSchoolCalls(api, 'school-alpha')[1].body).toEqual({ status: 'disabled' })
})

for (const theme of ['light', 'dark'] as const) {
  test(`invariant 3: an admin sees edit and admins greyed with reasons in ${theme}`, async ({
    page,
  }) => {
    await page.addInitScript((value) => {
      try {
        localStorage.setItem('dash26-theme', value)
      } catch {
        /* localStorage 禁用时主题退回媒体查询，下面的断言会明确失败。 */
      }
    }, theme)
    const api = await mockSchoolsApi(page, { role: 'admin' })
    await openSchools(page)
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme)

    const menu = await openRowMenu(page, '合成学校甲')
    const editItem = menu.getByRole('menuitem', { name: /编辑/ })
    const adminsItem = menu.getByRole('menuitem', { name: /管理员/ })
    await expect(editItem, 'denied, not hidden').toHaveAttribute('data-disabled')
    await expect(editItem).toContainText('仅 master 可编辑')
    await expect(adminsItem).toHaveAttribute('data-disabled')
    await expect(adminsItem).toContainText('仅 master 可管理学校管理员')

    const paint = await editItem.evaluate((element) => {
      const style = getComputedStyle(element)
      return { color: style.color, opacity: style.opacity }
    })
    expect(paint.color, `reason text must not be transparent in ${theme}`).not.toBe(
      'rgba(0, 0, 0, 0)',
    )
    expect(Number(paint.opacity), `reason text must stay opaque in ${theme}`).toBeGreaterThan(0)

    await expect(menu.getByRole('menuitem', { name: /删除/ }), 'no delete endpoint').toHaveCount(0)
    expect(adminListCalls(api, 'school-alpha'), 'a denied item opens nothing').toHaveLength(0)
  })
}

test('invariant 4: the admins modal pulls every cursor page before it lists', async ({ page }) => {
  const api = await mockSchoolsApi(page, {
    role: 'master',
    adminPages: { 'school-alpha': { '': [ADMINS[0]], c2: [ADMINS[1]] } },
    adminNextCursors: { 'school-alpha': { '': 'c2', c2: '' } },
  })
  await openSchools(page)

  const menu = await openRowMenu(page, '合成学校甲')
  await menu.getByRole('menuitem', { name: /管理员/ }).click()
  const dialog = page.getByRole('dialog', { name: '学校管理员' })
  await expect(dialog).toBeVisible()

  await expect.poll(() => adminListCalls(api, 'school-alpha').length).toBe(2)
  const calls = adminListCalls(api, 'school-alpha')
  expect(calls[0].params.limit).toBe('200')
  expect(calls[1].params.cursor).toBe('c2')
  await expect(dialog).toContainText('admin-one@example.com')
  await expect(dialog).toContainText('admin-two@example.com')
})

test('invariant 4: adding an admin needs a user id and refreshes the list', async ({ page }) => {
  const api = await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const menu = await openRowMenu(page, '合成学校甲')
  await menu.getByRole('menuitem', { name: /管理员/ }).click()
  const dialog = page.getByRole('dialog', { name: '学校管理员' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('契约无用户选择器')

  const add = dialog.getByRole('button', { name: '添加管理员' })
  await expect(add, 'a missing user id keeps add off').toBeDisabled()
  await dialog.getByLabel('用户 ID').fill('user-9')
  await expect(add).toBeEnabled()

  const before = adminListCalls(api, 'school-alpha').length
  await add.click()
  await expect.poll(() => adminAddCalls(api, 'school-alpha').length).toBe(1)
  expect(adminAddCalls(api, 'school-alpha')[0].body).toEqual({ user_id: 'user-9' })
  await expect.poll(() => adminListCalls(api, 'school-alpha').length).toBeGreaterThan(before)
  await expect(dialog).toContainText('user-9@example.com')
})

test('invariant 4: removing an admin waits for a confirmation', async ({ page }) => {
  const api = await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const menu = await openRowMenu(page, '合成学校甲')
  await menu.getByRole('menuitem', { name: /管理员/ }).click()
  const dialog = page.getByRole('dialog', { name: '学校管理员' })
  await expect(dialog).toBeVisible()

  const remove = dialog.getByRole('button', { name: '移除 admin-one@example.com' })
  await remove.click()
  const confirm = page.getByRole('dialog', { name: '移除管理员？' })
  await expect(confirm).toBeVisible()
  expect(adminRemoveCalls(api, 'school-alpha', 'user-2'), 'no request before confirmation').toHaveLength(
    0,
  )

  await confirm.getByRole('button', { name: '取消' }).click()
  await expect(confirm).toBeHidden()
  expect(adminRemoveCalls(api, 'school-alpha', 'user-2')).toHaveLength(0)
  await expect.poll(() => activeLabel(page), 'cancel returns focus').toBe(
    '移除 admin-one@example.com',
  )

  const before = adminListCalls(api, 'school-alpha').length
  await remove.click()
  await confirm.getByRole('button', { name: '确认移除' }).click()
  await expect.poll(() => adminRemoveCalls(api, 'school-alpha', 'user-2').length).toBe(1)
  await expect.poll(() => adminListCalls(api, 'school-alpha').length).toBeGreaterThan(before)
  await expect(dialog).not.toContainText('admin-one@example.com')
})

test('invariant 5: the cursor stack only walks back to visited pages', async ({ page }) => {
  const api = await mockSchoolsApi(page, {
    role: 'master',
    schoolPages: { '': [SCHOOLS[0]], c2: [SCHOOLS[1]] },
    schoolNextCursors: { '': 'c2', c2: '' },
  })
  await page.goto('/schools')
  await expect(page.getByRole('table', { name: '学校列表' })).toBeVisible()

  const previous = page.getByRole('button', { name: '上一页' })
  const next = page.getByRole('button', { name: '下一页' })
  await expect(previous, 'the first page has no backtrack slot').toBeDisabled()
  await expect(next).toBeEnabled()

  await next.click()
  await expect.poll(() => lastSchoolListCall(api)?.params.cursor).toBe('c2')
  await expect(page.getByRole('button', { name: '查看 合成学校乙' })).toBeVisible()
  await expect(previous).toBeEnabled()
  await expect(next, 'the last page has no next cursor').toBeDisabled()

  await previous.click()
  await expect.poll(() => lastSchoolListCall(api)?.params.cursor ?? 'absent').toBe('absent')
  await expect(page.getByRole('button', { name: '查看 合成学校甲' })).toBeVisible()
  await expect(previous).toBeDisabled()
})

test('invariant 5: an empty result says so', async ({ page }) => {
  await mockSchoolsApi(page, { role: 'master', schools: [] })
  await page.goto('/schools')
  await expect(page.getByText('没有学校')).toBeVisible()
})

test('a failed list shows the envelope and can retry', async ({ page }) => {
  const api = await mockSchoolsApi(page, {
    role: 'master',
    listError: { status: 502, code: 'upstream_unavailable', message: '学校服务不可用' },
  })
  await page.goto('/schools')
  await expect(page.getByText('upstream_unavailable · 学校服务不可用')).toBeVisible()

  const before = listSchoolCalls(api).length
  await page.getByRole('button', { name: '重试' }).click()
  await expect.poll(() => listSchoolCalls(api).length).toBeGreaterThan(before)
})

test('invariant 6: the detail drawer traps focus, closes on Escape and restores focus', async ({
  page,
}) => {
  await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const opener = page.getByRole('button', { name: '查看 合成学校甲' })
  await opener.focus()
  await page.keyboard.press('Enter')
  const drawer = page.getByRole('dialog', { name: '学校详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText('syn-alpha')
  await expect(drawer).toContainText('2026')
  expect(await focusIsInside(page, '.app-drawer'), 'focus starts inside the drawer').toBe(true)

  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect.poll(() => activeLabel(page)).toBe('查看 合成学校甲')

  await opener.click()
  await expect(drawer).toBeVisible()
  await page.locator('.app-drawer-backdrop').click({ position: { x: 40, y: 400 } })
  await expect(drawer).toBeHidden()
  await expect.poll(() => activeLabel(page)).toBe('查看 合成学校甲')
})

test('invariant 6: the create modal traps focus, closes on Escape and restores focus', async ({
  page,
}) => {
  await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const opener = page.getByRole('button', { name: '新建学校' })
  await opener.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '新建学校？' })
  await expect(dialog).toBeVisible()
  expect(await focusIsInside(page, '.app-modal'), 'focus starts inside the modal').toBe(true)

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect.poll(() => activeText(page)).toBe('新建学校')

  await opener.click()
  await expect(dialog).toBeVisible()
  await page.locator('.app-modal-backdrop').click({ position: { x: 8, y: 8 } })
  await expect(dialog).toBeHidden()
  await expect.poll(() => activeText(page)).toBe('新建学校')
})

test('invariant 6: the edit modal traps focus, closes on Escape and restores focus', async ({
  page,
}) => {
  await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const menu = await openRowMenu(page, '合成学校甲')
  await menu.getByRole('menuitem', { name: /编辑/ }).click()
  const dialog = page.getByRole('dialog', { name: '编辑学校？' })
  await expect(dialog).toBeVisible()
  expect(await focusIsInside(page, '.app-modal'), 'focus starts inside the modal').toBe(true)

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect.poll(() => activeLabel(page)).toBe('更多操作 合成学校甲')
})

test('invariant 6: the admins modal traps focus, closes on Escape and restores focus', async ({
  page,
}) => {
  await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const menu = await openRowMenu(page, '合成学校甲')
  await menu.getByRole('menuitem', { name: /管理员/ }).click()
  const dialog = page.getByRole('dialog', { name: '学校管理员' })
  await expect(dialog).toBeVisible()
  expect(await focusIsInside(page, '.app-modal'), 'focus starts inside the modal').toBe(true)

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect.poll(() => activeLabel(page)).toBe('更多操作 合成学校甲')
})

test('rows are 48px high, the content is not capped and the status wears a badge', async ({
  page,
}) => {
  await mockSchoolsApi(page, { role: 'master' })
  await openSchools(page)

  const row = page
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: '查看 合成学校甲' }) })
  // 行进入有 180ms 过渡：缩放期间的包围盒不是布局高度。等过渡落定再量。
  await expect
    .poll(async () => {
      const box = await row.boundingBox()
      return box === null ? null : Math.round(box.height)
    })
    .toBe(48)
  await expect(row.locator('.badge')).toHaveText('正常')

  const layout = await page.evaluate(() => {
    const content = document.querySelector('.content') as HTMLElement | null
    const card = document.querySelector('.card') as HTMLElement | null
    if (!content || !card) return null
    return {
      maxWidth: getComputedStyle(content).maxWidth,
      contentWidth: content.getBoundingClientRect().width,
      cardWidth: card.getBoundingClientRect().width,
    }
  })
  expect(layout, 'the content area is rendered').not.toBeNull()
  expect(layout!.maxWidth, 'content is not capped').toBe('none')
  expect(layout!.cardWidth, 'the card uses the content width').toBeGreaterThan(
    layout!.contentWidth - 100,
  )
})
