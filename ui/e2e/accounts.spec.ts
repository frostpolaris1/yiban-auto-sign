import { test, expect, type Page } from '@playwright/test'
import {
  ACCOUNTS,
  DEVICE_READ,
  deleteCalls,
  deviceRefreshCalls,
  createCalls,
  lastListCall,
  listCalls,
  locationCalls,
  mockAccountsApi,
  patchCalls,
  postCalls,
  restoreCalls,
} from './support/fake-accounts-api'

/**
 * 账号页的 9 条不变式。
 *
 * 1. 筛选变更进请求；清空即移除参数。
 * 2. 游标栈只回退已访问页；无回退位时上一页禁用。
 * 3. 无权限操作置灰并给出可见原因，明暗两主题都可见。
 * 4. 写操作必经确认模态；确认前不发请求。
 * 5. 抽屉打开焦点进入；Esc 与遮罩关闭并还原焦点。
 * 6. 编辑（PATCH）只送填写字段；空表单不可提交。
 * 7. 位置策略：auto 不送坐标；fixed 过 WGS84 范围；master 无权置灰。
 * 8. 删除必经确认；确认前零请求；行消失后焦点落筛选框。
 * 9. 关闭控件是 24×24 的可见目标，键盘焦点有 2px 实线环。
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

async function openAccounts(page: Page) {
  await page.goto('/accounts')
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()
  await expect(page.getByRole('button', { name: '查看 138****8000' })).toBeVisible()
}

function rowFor(page: Page, phone: string) {
  return page
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: `查看 ${phone}` }) })
}

test('invariant 1: each filter rides the request and clearing removes it', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  await expect
    .poll(() => lastListCall(api)?.params.status ?? 'absent')
    .toBe('absent')
  await expect.poll(() => lastListCall(api)?.params.school_id ?? 'absent').toBe('absent')
  await expect.poll(() => lastListCall(api)?.params.q ?? 'absent').toBe('absent')

  const q = page.getByLabel('手机号筛选')
  await expect(q).toHaveAttribute(
    'placeholder',
    '手机号：前 1–3 位 / 后 4 位 / 全 11 位',
  )

  await page.getByLabel('状态筛选').selectOption('active')
  await expect.poll(() => lastListCall(api)?.params.status).toBe('active')

  await expect(page.getByLabel('学校筛选').locator('option')).toHaveCount(3)
  await page.getByLabel('学校筛选').selectOption('school-b')
  await expect.poll(() => lastListCall(api)?.params.school_id).toBe('school-b')

  await q.fill('1380')
  await expect.poll(() => lastListCall(api)?.params.q).toBe('1380')

  await q.fill('')
  await expect.poll(() => lastListCall(api)?.params.q ?? 'absent').toBe('absent')

  await page.getByLabel('状态筛选').selectOption('')
  await expect.poll(() => lastListCall(api)?.params.status ?? 'absent').toBe('absent')

  await page.getByLabel('学校筛选').selectOption('')
  await expect.poll(() => lastListCall(api)?.params.school_id ?? 'absent').toBe('absent')
})

test('invariant 2: the cursor stack only walks back to visited pages', async ({ page }) => {
  const api = await mockAccountsApi(page, {
    role: 'master',
    pages: { '': [ACCOUNTS[0]], c2: [ACCOUNTS[1]], c3: [ACCOUNTS[2]] },
    nextCursors: { '': 'c2', c2: 'c3', c3: '' },
  })
  await openAccounts(page)

  const previous = page.getByRole('button', { name: '上一页' })
  const next = page.getByRole('button', { name: '下一页' })

  await expect(previous, 'the first page has no backtrack slot').toBeDisabled()
  await expect(next).toBeEnabled()

  await next.click()
  await expect.poll(() => lastListCall(api)?.params.cursor).toBe('c2')
  await expect(page.getByRole('button', { name: '查看 139****9000' })).toBeVisible()
  await expect(previous).toBeEnabled()

  await next.click()
  await expect.poll(() => lastListCall(api)?.params.cursor).toBe('c3')
  await expect(page.getByRole('button', { name: '查看 135****0001' })).toBeVisible()
  await expect(next, 'the last page has no next cursor').toBeDisabled()

  await previous.click()
  await expect.poll(() => lastListCall(api)?.params.cursor).toBe('c2')
  await expect(page.getByRole('button', { name: '查看 139****9000' })).toBeVisible()

  await previous.click()
  await expect.poll(() => lastListCall(api)?.params.cursor ?? 'absent').toBe('absent')
  await expect(page.getByRole('button', { name: '查看 138****8000' })).toBeVisible()
  await expect(previous, 'back at the first page').toBeDisabled()
})

test('invariant 2: a filter change on page two returns to the first page', async ({ page }) => {
  const api = await mockAccountsApi(page, {
    role: 'master',
    pages: { '': [ACCOUNTS[0]], c2: [ACCOUNTS[1]] },
    nextCursors: { '': 'c2', c2: '' },
  })
  await openAccounts(page)

  await page.getByRole('button', { name: '下一页' }).click()
  await expect.poll(() => lastListCall(api)?.params.cursor).toBe('c2')
  await expect(page.locator('.data-foot-info')).toContainText('第 2 页')

  await page.getByLabel('状态筛选').selectOption('active')
  await expect.poll(() => lastListCall(api)?.params.status).toBe('active')
  await expect.poll(() => lastListCall(api)?.params.cursor ?? 'absent').toBe('absent')
  await expect(page.locator('.data-foot-info')).toContainText('第 1 页')
})

test('invariant 2: the next page after a filter change uses the new cursor chain', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, {
    role: 'master',
    pages: { '': [ACCOUNTS[0]], c2: [ACCOUNTS[1]] },
    nextCursors: { '': 'c2', c2: '' },
  })
  await openAccounts(page)

  await page.getByLabel('状态筛选').selectOption('active')
  await expect.poll(() => lastListCall(api)?.params.status).toBe('active')
  await expect.poll(() => lastListCall(api)?.params.cursor ?? 'absent').toBe('absent')

  await page.getByRole('button', { name: '下一页' }).click()
  await expect.poll(() => lastListCall(api)?.params.cursor).toBe('c2')
  expect(lastListCall(api)?.params.status, 'the new filter stays on the next page').toBe('active')
})

for (const theme of ['light', 'dark'] as const) {
  test(`invariant 3: a denied control keeps a visible reason in ${theme}`, async ({ page }) => {
    await page.addInitScript((value) => {
      try {
        localStorage.setItem('dash26-theme', value)
      } catch {
        /* localStorage 禁用时主题退回媒体查询，下面的断言会明确失败。 */
      }
    }, theme)
    await mockAccountsApi(page, { role: 'user' })
    await openAccounts(page)
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme)

    await expect(page.getByLabel('学校筛选')).toBeDisabled()

    const note = page.locator('.app-deny-note').first()
    await expect(note).toBeVisible()
    await expect(note).toContainText('需要管理员权限')
    const paint = await note.evaluate((element) => {
      const style = getComputedStyle(element)
      return { color: style.color, opacity: style.opacity }
    })
    expect(paint.color, `reason text must not be transparent in ${theme}`).not.toBe(
      'rgba(0, 0, 0, 0)',
    )
    expect(Number(paint.opacity), `reason text must stay opaque in ${theme}`).toBeGreaterThan(0)

    await expect(page.getByRole('button', { name: '验证 138****8000' })).toBeDisabled()
    await expect(page.getByRole('button', { name: '评审 138****8000' })).toBeDisabled()
    await expect(page.getByRole('button', { name: '签到 138****8000' })).toBeEnabled()
    await expect(page.getByRole('button', { name: '暂停 139****9000' })).toBeEnabled()

    await expect(page.getByRole('button', { name: '暂停 138****8000' })).toBeDisabled()
    await expect(rowFor(page, '138****8000').locator('.app-deny-note')).toContainText(
      '仅正常状态可暂停',
    )

    await expect(page.getByRole('button', { name: '恢复 135****0001' })).toBeDisabled()
    await expect(rowFor(page, '135****0001').locator('.app-deny-note')).toContainText(
      '该暂停种类只能由对应角色恢复',
    )
  })
}

test('invariant 3: an admin may verify, review and filter by school', async ({ page }) => {
  await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)

  await expect(page.getByLabel('学校筛选')).toBeEnabled()
  await expect(page.getByRole('button', { name: '验证 138****8000' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '评审 138****8000' })).toBeEnabled()

  await expect(page.getByRole('button', { name: '暂停 139****9000' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '恢复 135****0001' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '暂停 138****8000' })).toBeDisabled()
  await expect(rowFor(page, '138****8000').locator('.app-deny-note')).toContainText(
    '仅正常状态可暂停',
  )
})

test('invariant 4: pause waits for confirmation and sends the flag', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  const drawer = page.getByRole('dialog', { name: '账号详情' })
  await page.getByRole('button', { name: '查看 139****9000' }).click()
  await expect(drawer).toContainText('正常')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  const listBefore = listCalls(api).length
  const pauseButton = page.getByRole('button', { name: '暂停 139****9000' })

  await pauseButton.click()
  const dialog = page.getByRole('dialog', { name: '暂停账号？' })
  await expect(dialog).toBeVisible()
  expect(postCalls(api, 'acc-2', 'pause'), 'no request before confirmation').toHaveLength(0)

  await dialog.getByRole('button', { name: '取消' }).click()
  await expect(dialog).toBeHidden()
  expect(postCalls(api, 'acc-2', 'pause')).toHaveLength(0)
  await expect.poll(() => activeLabel(page), 'cancel returns focus').toBe('暂停 139****9000')

  await pauseButton.click()
  await dialog.getByRole('button', { name: '确认暂停' }).click()
  await expect.poll(() => postCalls(api, 'acc-2', 'pause').length).toBe(1)
  expect(postCalls(api, 'acc-2', 'pause')[0].body).toEqual({ paused: true })
  await expect
    .poll(() => page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? ''))
    .toMatch(/^(暂停|恢复) 139\*\*\*\*9000$/)

  // 成功写操作让列表与详情缓存失效：详情重开必须显示新状态。
  await expect.poll(() => listCalls(api).length).toBeGreaterThan(listBefore)
  await page.getByRole('button', { name: '查看 139****9000' }).click()
  await expect(drawer).toContainText('管理员暂停')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  await page.getByRole('button', { name: '恢复 135****0001' }).click()
  const resume = page.getByRole('dialog', { name: '恢复账号？' })
  await expect(resume).toBeVisible()
  await resume.getByRole('button', { name: '确认恢复' }).click()
  await expect.poll(() => postCalls(api, 'acc-3', 'pause').length).toBe(1)
  expect(postCalls(api, 'acc-3', 'pause')[0].body).toEqual({ paused: false })
})

test('invariant 4: sign waits for confirmation and shows the conflict envelope', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, {
    role: 'user',
    signError: { status: 409, code: 'window_closed', message: '签到窗口已关闭' },
  })
  await openAccounts(page)

  await page.getByRole('button', { name: '签到 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '立即签到？' })
  await expect(dialog).toBeVisible()
  expect(postCalls(api, 'acc-1', 'sign'), 'no request before confirmation').toHaveLength(0)

  await dialog.getByRole('button', { name: '确认签到' }).click()
  await expect.poll(() => postCalls(api, 'acc-1', 'sign').length).toBe(1)
  await expect(dialog).toContainText('签到窗口已关闭')
  await expect(dialog).toContainText('window_closed')
})

test('invariant 4: verify waits for confirmation and reports the conclusion', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)

  await page.getByRole('button', { name: '验证 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '验证凭据？' })
  await expect(dialog).toBeVisible()
  expect(postCalls(api, 'acc-1', 'verify'), 'no request before confirmation').toHaveLength(0)

  await dialog.getByRole('button', { name: '确认验证' }).click()
  await expect.poll(() => postCalls(api, 'acc-1', 'verify').length).toBe(1)
  await expect(dialog).toContainText('验证通过')
})

for (const conflict of [
  { code: 'already_signed', message: '今日已出结论' },
  { code: 'sign_running', message: '签到正在运行' },
] as const) {
  test(`sign reports ${conflict.code}`, async ({ page }) => {
    const api = await mockAccountsApi(page, {
      role: 'user',
      signError: { status: 409, code: conflict.code, message: conflict.message },
    })
    await openAccounts(page)

    await page.getByRole('button', { name: '签到 138****8000' }).click()
    const dialog = page.getByRole('dialog', { name: '立即签到？' })
    await dialog.getByRole('button', { name: '确认签到' }).click()
    await expect.poll(() => postCalls(api, 'acc-1', 'sign').length).toBe(1)
    await expect(dialog).toContainText(conflict.code)
    await expect(dialog).toContainText(conflict.message)
  })
}

test('verify reports upstream_unavailable', async ({ page }) => {
  const api = await mockAccountsApi(page, {
    role: 'admin',
    verifyError: { status: 502, code: 'upstream_unavailable', message: '平台不可达' },
  })
  await openAccounts(page)

  await page.getByRole('button', { name: '验证 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '验证凭据？' })
  await dialog.getByRole('button', { name: '确认验证' }).click()
  await expect.poll(() => postCalls(api, 'acc-1', 'verify').length).toBe(1)
  await expect(dialog).toContainText('upstream_unavailable')
  await expect(dialog).toContainText('平台不可达')
})

test('verify reports a platform rejection without a transport error', async ({ page }) => {
  const api = await mockAccountsApi(page, {
    role: 'admin',
    verifyResult: { ok: false, code: 'credential_invalid', message: '凭据无效' },
  })
  await openAccounts(page)

  await page.getByRole('button', { name: '验证 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '验证凭据？' })
  await dialog.getByRole('button', { name: '确认验证' }).click()
  await expect.poll(() => postCalls(api, 'acc-1', 'verify').length).toBe(1)
  await expect(dialog).toContainText('验证失败：credential_invalid')
  await expect(dialog).toContainText('凭据无效')
})

test('invariant 4: review needs a reason before it may reject', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)

  await page.getByRole('button', { name: '评审 138****8000' }).click()
  const dialog = page.getByRole('dialog', { name: '评审账号？' })
  await expect(dialog).toBeVisible()
  expect(postCalls(api, 'acc-1', 'review'), 'no request before confirmation').toHaveLength(0)

  await dialog.getByLabel('评审结论').selectOption('reject')
  const confirm = dialog.getByRole('button', { name: '提交评审' })
  await expect(confirm, 'reject requires a reason').toBeDisabled()
  expect(postCalls(api, 'acc-1', 'review')).toHaveLength(0)

  await dialog.getByLabel('驳回理由').fill('合成驳回理由')
  await expect(confirm).toBeEnabled()
  await confirm.click()
  await expect.poll(() => postCalls(api, 'acc-1', 'review').length).toBe(1)
  expect(postCalls(api, 'acc-1', 'review')[0].body).toEqual({
    decision: 'reject',
    reason: '合成驳回理由',
  })
})

test('invariant 5: the drawer traps focus, closes on Escape and restores focus', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  const opener = page.getByRole('button', { name: '查看 138****8000' })
  await opener.focus()
  await page.keyboard.press('Enter')

  const drawer = page.getByRole('dialog', { name: '账号详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText('138****8000')
  await expect(drawer).toContainText('合成学校甲')
  expect(await focusIsInside(page, '.app-drawer'), 'focus starts inside the drawer').toBe(true)
  expect(
    api.calls.some((call) => call.method === 'GET' && call.path === '/api/accounts/acc-1'),
    'the drawer reads the detail endpoint',
  ).toBe(true)

  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect.poll(() => activeLabel(page)).toBe('查看 138****8000')

  await opener.click()
  await expect(drawer).toBeVisible()
  await page.locator('.app-drawer-backdrop').click({ position: { x: 40, y: 400 } })
  await expect(drawer).toBeHidden()
  await expect.poll(() => activeLabel(page)).toBe('查看 138****8000')
})

test('the whole row opens the drawer as well', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  await page.getByRole('cell', { name: 'school-b', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '账号详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText('139****9000')
})

test('rows are 48px high and the status wears a badge', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  const row = page.getByRole('row').filter({ has: page.getByRole('button', { name: '查看 138****8000' }) })
  // 行为用例开减少动效；这里仍轮询到布局高度稳定，钉的是行高 48px。
  await expect
    .poll(async () => {
      const box = await row.boundingBox()
      return box === null ? null : Math.round(box.height)
    })
    .toBe(48)
  await expect(row.locator('.badge')).toHaveText('待审核')

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

test('an empty result says so instead of showing a blank table', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master', accounts: [] })
  await page.goto('/accounts')
  await expect(page.getByText('没有匹配的账号')).toBeVisible()
})

test('the sidebar reaches the accounts page', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })
  await page.goto('/')
  await page.locator('.d-sidebar').getByRole('link', { name: '账号', exact: true }).click()
  await expect(page).toHaveURL(/\/accounts$/)
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()
})

async function openRowMenu(page: Page, phone: string) {
  await page.getByRole('button', { name: `更多操作 ${phone}` }).click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  return menu
}

test('invariant 6: PATCH sends only the filled fields', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  const menu = await openRowMenu(page, '138****8000')
  await menu.getByRole('menuitem', { name: /编辑/ }).click()
  const dialog = page.getByRole('dialog', { name: '编辑账号？' })
  await expect(dialog).toBeVisible()
  expect(patchCalls(api, 'acc-1'), 'no request before save').toHaveLength(0)

  const save = dialog.getByRole('button', { name: '保存修改' })
  await expect(save, 'an empty form stays closed').toBeDisabled()

  await dialog.getByLabel('设备识别码').fill('dev-1')
  await expect(save).toBeEnabled()
  await save.click()
  await expect.poll(() => patchCalls(api, 'acc-1').length).toBe(1)
  expect(patchCalls(api, 'acc-1')[0].body).toEqual({ device_code: 'dev-1' })

  // 第二次只改手机号：掩码号随列表刷新更新。
  const menu2 = await openRowMenu(page, '138****8000')
  await menu2.getByRole('menuitem', { name: /编辑/ }).click()
  const dialog2 = page.getByRole('dialog', { name: '编辑账号？' })
  await dialog2.getByLabel('手机号').fill('13800138001')
  await dialog2.getByRole('button', { name: '保存修改' }).click()
  await expect.poll(() => patchCalls(api, 'acc-1').length).toBe(2)
  expect(patchCalls(api, 'acc-1')[1].body).toEqual({ phone: '13800138001' })
  await expect(page.getByRole('button', { name: '查看 138****8001' })).toBeVisible()
})

test('invariant 6: an invalid phone blocks the edit form', async ({ page }) => {
  await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  const menu = await openRowMenu(page, '138****8000')
  await menu.getByRole('menuitem', { name: /编辑/ }).click()
  const dialog = page.getByRole('dialog', { name: '编辑账号？' })

  await dialog.getByLabel('手机号').fill('123')
  await expect(dialog.getByRole('button', { name: '保存修改' })).toBeDisabled()
  await expect(dialog).toContainText('11 位')

  await dialog.getByLabel('手机号').fill('   ')
  await expect(dialog.getByRole('button', { name: '保存修改' })).toBeDisabled()
})

for (const theme of ['light', 'dark'] as const) {
  test(`invariant 7: location is denied to master with a readable reason in ${theme}`, async ({
    page,
  }) => {
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

    const menu = await openRowMenu(page, '138****8000')
    const locationItem = menu.getByRole('menuitem', { name: /位置/ })
    await expect(locationItem, 'denied, not hidden').toHaveAttribute('data-disabled')
    await expect(locationItem).toContainText('仅 user/admin 可设置')
    const paint = await locationItem.evaluate((element) => {
      const style = getComputedStyle(element)
      return { color: style.color, opacity: style.opacity }
    })
    expect(paint.color, `reason must not be transparent in ${theme}`).not.toBe('rgba(0, 0, 0, 0)')
    expect(Number(paint.opacity), `reason must stay readable in ${theme}`).toBeGreaterThan(0)

    await expect(menu.getByRole('menuitem', { name: /编辑/ })).not.toHaveAttribute('data-disabled')
    await expect(menu.getByRole('menuitem', { name: /删除/ })).not.toHaveAttribute('data-disabled')
  })
}

test('invariant 7: location auto sends no coordinates', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)

  const menu = await openRowMenu(page, '138****8000')
  await menu.getByRole('menuitem', { name: /位置/ }).click()
  const dialog = page.getByRole('dialog', { name: '设置位置策略？' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('当前策略不可读取')
  expect(locationCalls(api, 'acc-1'), 'no request before save').toHaveLength(0)

  await expect(dialog.getByLabel('经度')).toBeDisabled()
  await expect(dialog.getByLabel('纬度')).toBeDisabled()

  const save = dialog.getByRole('button', { name: '保存策略' })
  await expect(save).toBeEnabled()
  await save.click()
  await expect.poll(() => locationCalls(api, 'acc-1').length).toBe(1)
  expect(locationCalls(api, 'acc-1')[0].body).toEqual({ mode: 'auto' })
})

test('invariant 7: fixed location validates and caches for prefill', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)

  const menu = await openRowMenu(page, '138****8000')
  await menu.getByRole('menuitem', { name: /位置/ }).click()
  const dialog = page.getByRole('dialog', { name: '设置位置策略？' })
  await dialog.getByLabel('定位模式').selectOption('fixed')

  const save = dialog.getByRole('button', { name: '保存策略' })
  await expect(save, 'missing coordinates stay closed').toBeDisabled()

  await dialog.getByLabel('经度').fill('200')
  await expect(save).toBeDisabled()
  await dialog.getByLabel('经度').fill('1.5')
  await dialog.getByLabel('纬度').fill('95')
  await expect(save).toBeDisabled()
  await dialog.getByLabel('纬度').fill('2.5')
  await dialog.getByLabel('设置理由').fill('合成理由')
  await expect(save).toBeEnabled()
  await save.click()
  await expect.poll(() => locationCalls(api, 'acc-1').length).toBe(1)
  expect(locationCalls(api, 'acc-1')[0].body).toEqual({
    mode: 'fixed',
    lng: 1.5,
    lat: 2.5,
    reason: '合成理由',
  })

  // 成功响应进会话内缓存：再开预填，不再盲态。
  const menu2 = await openRowMenu(page, '138****8000')
  await menu2.getByRole('menuitem', { name: /位置/ }).click()
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('定位模式')).toHaveValue('fixed')
  await expect(dialog.getByLabel('经度')).toHaveValue('1.5')
  await expect(dialog.getByLabel('纬度')).toHaveValue('2.5')
  await expect(dialog.getByLabel('设置理由')).toHaveValue('合成理由')
  await expect(dialog).not.toContainText('当前策略不可读取')
})

test('invariant 8: delete waits for confirmation and removes the row', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'user' })
  await openAccounts(page)

  const menu = await openRowMenu(page, '135****0001')
  await menu.getByRole('menuitem', { name: /删除/ }).click()
  const dialog = page.getByRole('dialog', { name: '删除账号？' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('软删除')
  await expect(dialog).toContainText('凭据将被级联隐藏')
  expect(deleteCalls(api, 'acc-3'), 'no request before confirmation').toHaveLength(0)

  await dialog.getByRole('button', { name: '取消' }).click()
  await expect(dialog).toBeHidden()
  expect(deleteCalls(api, 'acc-3')).toHaveLength(0)
  await expect.poll(() => activeLabel(page), 'cancel returns focus').toBe('更多操作 135****0001')

  await page.getByRole('button', { name: '查看 135****0001' }).click()
  const drawer = page.getByRole('dialog', { name: '账号详情' })
  await expect(drawer).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  const menu2 = await openRowMenu(page, '135****0001')
  await menu2.getByRole('menuitem', { name: /删除/ }).click()
  await dialog.getByRole('button', { name: '确认删除' }).click()
  await expect.poll(() => deleteCalls(api, 'acc-3').length).toBe(1)
  await expect.poll(() => page.getByRole('button', { name: '查看 135****0001' }).count()).toBe(0)
  await expect(drawer).toBeHidden()
  await expect
    .poll(() => activeLabel(page), 'the row is gone; focus lands on the filter')
    .toBe('手机号筛选')
})

test('invariant 9: the close control is a 24x24 target with a keyboard focus ring', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  await page.getByRole('button', { name: '查看 139****9000' }).click()
  const drawer = page.getByRole('dialog', { name: '账号详情' })
  const drawerClose = drawer.getByRole('button', { name: '关闭详情' })
  await expect(drawerClose).toBeVisible()
  const drawerBox = await drawerClose.boundingBox()
  expect(drawerBox?.width).toBe(24)
  expect(drawerBox?.height).toBe(24)
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  await page.getByRole('button', { name: '暂停 139****9000' }).click()
  const modal = page.getByRole('dialog', { name: '暂停账号？' })
  const modalClose = modal.getByRole('button', { name: '关闭弹窗' })
  await expect(modalClose).toBeVisible()
  const modalBox = await modalClose.boundingBox()
  expect(modalBox?.width).toBe(24)
  expect(modalBox?.height).toBe(24)

  // 用键盘离开再回来，焦点可见性按键盘口径判定，不吃浏览器默认 outline。
  await page.keyboard.press('Tab')
  await page.keyboard.press('Shift+Tab')
  const outline = await modalClose.evaluate((element) => {
    const style = getComputedStyle(element)
    return { style: style.outlineStyle, width: style.outlineWidth }
  })
  expect(outline).toEqual({ style: 'solid', width: '2px' })

  // 悬停要有可见底：无 hover 反馈的裸图标不算可用目标。
  await modalClose.hover()
  await expect
    .poll(() => modalClose.evaluate((element) => getComputedStyle(element).backgroundColor))
    .not.toBe('rgba(0, 0, 0, 0)')
})

// ---------------------------------------------------------------------------
// UX-2（8v8）：账号提交双形状。两页向导、单页绑定用户、设备码只读、恢复。
//
// 断言用户可见结果与请求参数，不钉内部调用顺序。
// 设备码在界面上是文本不是输入框：全文只在服务端，响应只给掩码。
// ---------------------------------------------------------------------------

const CREATE_FORM_LABELS = ['学校', '手机号', '易班口令'] as const

/** user 两页向导：填第 1 页并提交，进入第 2 页。表单字段一律在 dialog 内取。 */
async function submitWizardPageOne(page: Page, phone = '13800000000') {
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('学校').selectOption({ label: '合成学校甲' })
  await dialog.getByLabel('手机号').fill(phone)
  await dialog.getByLabel('易班口令').fill('wizard-pass-1')
  await dialog.getByRole('button', { name: '提交' }).click()
  // 第 2 页的可达标志是「跳过」：成功与失败两条路径都在这一页。
  await expect(dialog.getByRole('button', { name: '跳过' })).toBeVisible()
}

test('ux2 user: the wizard has two pages, zero email fields and a readonly device code', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'user' })
  await openAccounts(page)

  await page.getByRole('button', { name: '新建账号' }).click()
  await expect(page.getByRole('heading', { name: '新建账号' })).toBeVisible()

  // 第 1 页只有学校、手机号、口令；零邮箱字段、零绑定用户文案（8v8⑤）。
  const dialog = page.getByRole('dialog')
  for (const label of CREATE_FORM_LABELS) {
    await expect(dialog.locator('label').filter({ hasText: label })).toBeVisible()
  }
  await expect(page.getByLabel(/邮箱/)).toHaveCount(0)
  await expect(page.getByText('绑定用户')).toHaveCount(0)

  await submitWizardPageOne(page)

  // 建号请求只带三键，不带 owner_user_id：留空即自有。
  await expect.poll(() => createCalls(api).length).toBe(1)
  const create = createCalls(api)[0]
  expect(create.body).toMatchObject({ phone: '13800000000' })
  expect(create.body).not.toHaveProperty('owner_user_id')

  // 第 2 页：设备码是只读文本（非输入框）且带来源说明。
  const dialog2 = page.getByRole('dialog')
  await expect(dialog2.getByText(DEVICE_READ.device_code_masked)).toBeVisible({
    timeout: 10_000,
  })
  await expect(dialog2.getByText('来自易班平台登记，不可修改')).toBeVisible()
  await expect(page.locator('#account-create-device input, #account-create-device textarea')).toHaveCount(0)
  await expect(deviceRefreshCalls(api, 'acc-new')).toHaveLength(1)
})

test('ux2 user: a failed device read stays non-blocking and offers the later entry', async ({
  page,
}) => {
  await mockAccountsApi(page, {
    role: 'user',
    deviceRefreshError: { status: 502, code: 'upstream_unavailable', message: '平台不可用' },
  })
  await openAccounts(page)

  await page.getByRole('button', { name: '新建账号' }).click()
  await submitWizardPageOne(page)

  await expect(page.getByRole('dialog').getByText('设备信息读取失败')).toBeVisible()
  await expect(page.getByRole('dialog').getByText('可稍后在账号详情中补录')).toBeVisible()
  // 跳过仍可达：完成不被阻断。
  await page.getByRole('dialog').getByRole('button', { name: '跳过' }).click()
  await expect(page.getByText('已创建 138****0000')).toBeVisible()
})

test('ux2 user: skip lands on the drawer device block with a reachable refresh', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'user' })
  await openAccounts(page)

  await page.getByRole('button', { name: '新建账号' }).click()
  await submitWizardPageOne(page)
  await page.getByRole('dialog').getByRole('button', { name: '跳过' }).click()
  await expect(page.getByText('已创建 138****0000')).toBeVisible()

  // 补录入口：详情抽屉的设备块与「刷新设备」可达（8v8③）。
  await page.getByRole('button', { name: '查看 138****0000' }).click()
  await expect(page.getByRole('dialog', { name: '账号详情' }).getByText('设备信息')).toBeVisible()
  await page.getByRole('dialog', { name: '账号详情' }).getByRole('button', { name: '刷新设备' }).click()
  await expect(
    page.getByRole('dialog', { name: '账号详情' }).getByText(DEVICE_READ.device_code_masked),
  ).toBeVisible()
  await expect(deviceRefreshCalls(api, 'acc-new')).toHaveLength(2)
})

test('ux2 master: the single form resolves owner by email lookup and leaves it blank for self', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  // 命中：显示掩码邮箱，提交携带 owner_user_id。
  await page.getByRole('button', { name: '新建账号' }).click()
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('学校').selectOption({ label: '合成学校甲' })
  await dialog.getByLabel('手机号').fill('13800000000')
  await dialog.getByLabel('易班口令').fill('single-pass-1')
  await dialog.getByLabel('绑定用户（选填）').fill('member.target')
  await expect(dialog.getByText('m***@example.com')).toBeVisible()
  await dialog.getByRole('button', { name: '提交' }).click()
  await expect.poll(() => createCalls(api).length).toBe(1)
  expect(createCalls(api)[0].body).toMatchObject({ owner_user_id: 'user-target' })
  await expect(page.getByText('已创建 138****0000')).toBeVisible()

  // 重开表单。未注册：内联提示，不静默建号。
  await page.getByRole('button', { name: '新建账号' }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('学校').selectOption({ label: '合成学校甲' })
  await dialog.getByLabel('手机号').fill('13800000000')
  await dialog.getByLabel('易班口令').fill('single-pass-1')
  await dialog.getByLabel('绑定用户（选填）').fill('ghost@')
  await expect(dialog.getByText('该邮箱未注册，请先在用户管理建号')).toBeVisible()

  // 留空：默认自有，请求不带 owner_user_id。
  await dialog.getByLabel('绑定用户（选填）').fill('')
  await expect(dialog.getByText('默认归属自己')).toBeVisible()
  await dialog.getByRole('button', { name: '提交' }).click()
  await expect.poll(() => createCalls(api).length).toBe(2)
  expect(createCalls(api)[1].body).not.toHaveProperty('owner_user_id')
})

test('ux2 master: the deleted filter reveals restore and the row returns to normal', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)

  await page.getByRole('checkbox', { name: '含已删除' }).check()
  await expect.poll(() => lastListCall(api)?.params.include_deleted ?? 'absent').toBe('true')

  const deletedRow = rowFor(page, '131****0000')
  await expect(deletedRow.getByText('已删除')).toBeVisible()

  await deletedRow.getByRole('button', { name: '更多操作 131****0000' }).click()
  // 菜单渲染在行外的门户里：从页面根取菜单项。
  await page.getByRole('menuitem', { name: '恢复' }).click()
  await expect(restoreCalls(api, 'acc-del')).toHaveLength(1)
  await expect.poll(() => lastListCall(api)?.params.include_deleted ?? 'absent').toBe('true')
  // 恢复后行回原状态：软删徽标消失。
  await expect(rowFor(page, '131****0000').getByText('已删除')).toHaveCount(0)
})

test('ux2 user: no deleted filter and no restore entry', async ({ page }) => {
  await mockAccountsApi(page, { role: 'user' })
  await openAccounts(page)
  await expect(page.getByRole('checkbox', { name: '含已删除' })).toHaveCount(0)
})
