import { test, expect, type Page } from '@playwright/test'
import {
  PLAN_ALPHA,
  SCHEDULE_DAY,
  lastWindowsCall,
  mockScheduleApi,
  overrideCalls,
  planCalls,
  previewCalls,
  scheduleCalls,
  timePrefGetCalls,
  windowsCalls,
  windowsRefreshCalls,
} from './support/fake-schedule-api'

/**
 * 系统设置（`/system` 签到计划分区）的不变式；时间偏好卡已不存在于管理台（见不变式 6）。
 *
 * 1. 路由与角色：master/admin 从侧栏「系统设置」进 `/system`（页顶锚链含「计划」）；
 *    `/schedule`（含 `?day=`）302 重定向到 `/system`（保 day，锚定 `#plans`），
 *    非法 `?day=` 丢弃并回落今日；user 直达 `/system` 与 `/schedule` 一并 302 到
 *    用户端 `/app`：管理台壳与计划表都不渲染，且零 schedule 请求。
 * 2. 窗口一览：日期改写请求 day；「重新发现」改发 POST windows/refresh；
 *    null 窗口行禁用「查看计划」。
 * 3. 预览：打开即发 preview，参数与所点行一致；停用任务显示提示且两个写动作禁用。
 * 4. Build：确认前零 POST；确认体只含契约三字段；409 文案如实；成功后列表刷新。
 * 5. Override：reason 空或超长、end<=start 由前端拒绝；确认前零 POST；
 *    确认体只含契约六字段；成功后展示重建计划（version≠0）。
 * 6. 时间偏好：user 专属界面在用户端 `/app`（不属本文件）；管理台不渲染该卡，
 *    也不读 `/api/me/time-pref`。
 * 7. 焦点：预览、覆盖、确认三个模态焦点进入与还原，Esc 与遮罩关闭。
 *
 * 断言用户可见结果，不钉内部调用顺序。
 */

async function focusInDialog(page: Page, title: string): Promise<boolean> {
  return page.evaluate((name) => {
    const active = document.activeElement
    const dialog = active?.closest('[role="dialog"]')
    return Boolean(dialog && dialog.textContent?.includes(name))
  }, title)
}

async function activeText(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.textContent?.trim() ?? null)
}

async function activeLabel(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? null)
}

async function openSchedule(page: Page) {
  await page.goto(`/system?day=${SCHEDULE_DAY}`)
  await expect(page.getByRole('table', { name: '时间片窗口' })).toBeVisible()
}

async function openPreview(page: Page, school: string) {
  await page.getByRole('button', { name: `查看计划 ${school}` }).click()
  const dialog = page.getByRole('dialog', { name: '计划预览' })
  await expect(dialog).toBeVisible()
  return dialog
}

for (const role of ['master', 'admin'] as const) {
  test(`invariant 1: ${role} reaches /system#plans from the navigation`, async ({ page }) => {
    await mockScheduleApi(page, { role })
    await page.goto('/')
    const link = page.locator('.d-sidebar').getByRole('link', { name: '系统设置', exact: true })
    await expect(link).toHaveCount(1)
    await expect(
      page.locator('.d-sidebar').getByRole('link', { name: '签到计划', exact: true }),
    ).toHaveCount(0)

    await link.click()
    await expect(page).toHaveURL(/\/system$/)
    await expect(page.getByRole('heading', { level: 1 })).toContainText('系统设置')
    const anchor = page.getByRole('navigation', { name: '系统设置分区' }).getByRole('link', {
      name: '计划',
    })
    await expect(anchor).toHaveAttribute('href', '#plans')
    await anchor.click()
    await expect(page).toHaveURL(/\/system#plans$/)
    await expect(page.locator('#plans')).toBeVisible()
    await expect(page.getByRole('table', { name: '时间片窗口' })).toBeVisible()
  })
}

test('invariant 1: /schedule redirects to /system#plans preserving day and drops bogus day', async ({
  page,
}) => {
  const api = await mockScheduleApi(page, { role: 'master' })

  await page.goto(`/schedule?day=${SCHEDULE_DAY}`)
  await expect(page).toHaveURL(new RegExp(`/system\\?day=${SCHEDULE_DAY}#plans$`))
  await expect(page.getByRole('table', { name: '时间片窗口' })).toBeVisible()
  expect(lastWindowsCall(api)?.params.day).toBe(SCHEDULE_DAY)

  await page.goto('/schedule')
  await expect(page).toHaveURL(/\/system#plans$/)
  await expect(page.getByRole('table', { name: '时间片窗口' })).toBeVisible()

  await page.goto('/schedule?day=not-a-date')
  await expect(page).toHaveURL(/\/system#plans$/)

  await page.goto('/system?day=not-a-date')
  await expect(page.getByRole('table', { name: '时间片窗口' })).toBeVisible()
  expect(lastWindowsCall(api)?.params.day).toMatch(/^\d{4}-\d{2}-\d{2}$/)
})

test('invariant 1: a user is sent to /app from /system and /schedule and sends zero schedule requests', async ({
  page,
}) => {
  const api = await mockScheduleApi(page, { role: 'user' })

  for (const url of [`/system?day=${SCHEDULE_DAY}`, `/schedule?day=${SCHEDULE_DAY}`]) {
    await page.goto(url)
    await expect(page).toHaveURL(/\/app$/)
    await expect(page.locator('.uapp-bar'), 'the user app takes over').toBeVisible()
    await expect(page.locator('.d-sidebar'), 'the manager shell never renders').toHaveCount(0)
    await expect(page.locator('#plans')).toHaveCount(0)
    await expect(page.getByRole('table', { name: '时间片窗口' })).toHaveCount(0)
  }
  expect(scheduleCalls(api), 'a denied page sends no schedule request').toHaveLength(0)
})

test('invariant 2: the day rides the request and refresh re-discovers', async ({ page }) => {
  const api = await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)

  expect(lastWindowsCall(api)?.params.day).toBe(SCHEDULE_DAY)
  expect(lastWindowsCall(api)?.params.refresh, 'the first read uses the cache').toBeUndefined()

  await page.getByLabel('日期').fill('2026-11-02')
  await expect.poll(() => lastWindowsCall(api)?.params.day).toBe('2026-11-02')
  await expect(page).toHaveURL(/day=2026-11-02/)

  const before = windowsCalls(api).length
  await page.getByRole('button', { name: '重新发现', exact: true }).click()
  await expect.poll(() => windowsRefreshCalls(api).length).toBe(1)
  expect(windowsCalls(api).length).toBeGreaterThan(before)
  expect(lastWindowsCall(api)?.method).toBe('POST')
  expect(lastWindowsCall(api)?.path).toBe('/api/schedule/windows/refresh')
  expect(lastWindowsCall(api)?.params.day).toBe('2026-11-02')
})

test('invariant 2: a row without a platform window cannot open a plan', async ({ page }) => {
  await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)

  const alpha = page
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: '查看计划 合成学校甲' }) })
  await expect(alpha).toContainText('08:10–09:30')
  await expect(alpha).toContainText('平台')
  await expect(alpha).toContainText('已启用')

  const gamma = page
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: '查看计划 合成学校丙' }) })
  await expect(gamma).toContainText('回退')
  await expect(gamma).toContainText('已停用')

  const beta = page
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: '查看计划 合成学校乙' }) })
  await expect(beta).toContainText('平台未报窗口')
  await expect(page.getByRole('button', { name: '查看计划 合成学校乙' })).toBeDisabled()
  await expect(beta.getByText('—')).toBeVisible()
})

test('invariant 3: opening a preview reads exactly the clicked row', async ({ page }) => {
  const api = await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)

  const dialog = await openPreview(page, '合成学校甲')
  const call = previewCalls(api).at(-1)
  expect(call?.params, 'the preview carries the row identity').toEqual({
    day: SCHEDULE_DAY,
    school_id: 'school-alpha',
    task_id: 'task-daily',
  })

  await expect(dialog).toContainText('08:10')
  await expect(dialog).toContainText('09:30')
  await expect(dialog).toContainText('v0')
  await expect(dialog.locator('.app-metric')).toHaveCount(3)
  await expect(dialog.locator('.app-histogram-bar')).toHaveCount(6)
  await expect(dialog.getByRole('button', { name: '落库此计划' })).toBeEnabled()
  await expect(dialog.getByRole('button', { name: '写入覆盖' })).toBeEnabled()
})

test('invariant 3: a disabled task preview blocks both write actions', async ({ page }) => {
  await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)

  const dialog = await openPreview(page, '合成学校丙')
  await expect(dialog).toContainText('任务已停用，落库与覆盖将被拒绝。')
  await expect(dialog.getByRole('button', { name: '落库此计划' })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '写入覆盖' })).toBeDisabled()
})

test('invariant 4: build waits for confirmation and sends the contract fields', async ({ page }) => {
  // 落库响应带非零版本与缓存来源：预览模态必须显示落库后的状态。
  const api = await mockScheduleApi(page, {
    role: 'master',
    planResult: { ...PLAN_ALPHA, version: 7, source: 'cache' },
  })
  await openSchedule(page)
  const preview = await openPreview(page, '合成学校甲')

  await preview.getByRole('button', { name: '落库此计划' }).click()
  const confirm = page.getByRole('dialog', { name: '落库此计划？' })
  await expect(confirm).toBeVisible()
  expect(planCalls(api), 'no write before confirmation').toHaveLength(0)

  await confirm.getByRole('button', { name: '取消', exact: true }).click()
  await expect(confirm).toBeHidden()
  expect(planCalls(api)).toHaveLength(0)
  await expect(preview).toBeVisible()

  const before = windowsCalls(api).length
  await preview.getByRole('button', { name: '落库此计划' }).click()
  await confirm.getByRole('button', { name: '确认落库' }).click()
  await expect.poll(() => planCalls(api).length).toBe(1)
  expect(planCalls(api)[0].body).toEqual({
    day: SCHEDULE_DAY,
    school_id: 'school-alpha',
    task_id: 'task-daily',
  })
  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '已落库' })).toBeVisible()
  await expect.poll(() => windowsCalls(api).length).toBeGreaterThan(before)
  // 预览滞留写库前状态是缺陷：落库响应（v7/cache）必须已写进预览缓存。
  await expect(preview.getByText('v7')).toBeVisible()
  await expect(preview.getByText('缓存')).toBeVisible()
  // 预览保留（焦点链不断）：关闭仍还原到发起按钮。
  await expect(page.getByRole('dialog', { name: '计划预览' })).toBeVisible()
  await preview.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '计划预览' })).toBeHidden()
})

test('invariant 4: a 409 build reports the disabled task', async ({ page }) => {
  await mockScheduleApi(page, {
    role: 'master',
    planError: { status: 409, code: 'conflict', message: 'task disabled' },
  })
  await openSchedule(page)
  const preview = await openPreview(page, '合成学校甲')

  await preview.getByRole('button', { name: '落库此计划' }).click()
  const confirm = page.getByRole('dialog', { name: '落库此计划？' })
  await confirm.getByRole('button', { name: '确认落库' }).click()
  await expect(confirm.getByText('任务已停用')).toBeVisible()
  await expect(confirm).toBeVisible()
})

test('invariant 5: the override form rejects bad input before any request', async ({ page }) => {
  const api = await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)
  const preview = await openPreview(page, '合成学校甲')

  await preview.getByRole('button', { name: '写入覆盖' }).click()
  const dialog = page.getByRole('dialog', { name: '写入窗口覆盖？' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('开始时间')).toHaveValue('2026-10-28T08:10')
  await expect(dialog.getByLabel('结束时间')).toHaveValue('2026-10-28T09:30')

  const next = dialog.getByRole('button', { name: '下一步：确认覆盖' })
  await expect(next, 'an empty reason keeps the step off').toBeDisabled()
  await expect(dialog.getByText('修改原因必填')).toBeVisible()

  await dialog.getByLabel('修改原因').fill('合成调整窗口')
  await expect(next).toBeEnabled()

  await dialog.getByLabel('开始时间').fill('2026-10-28T09:30')
  await expect(dialog.getByText('结束时间必须晚于开始时间')).toBeVisible()
  await expect(next).toBeDisabled()

  await dialog.getByLabel('结束时间').fill('2026-10-28T09:20')
  await expect(dialog.getByText('结束时间必须晚于开始时间')).toBeVisible()
  await expect(next).toBeDisabled()

  await dialog.getByLabel('结束时间').fill('2026-10-28T10:00')
  await dialog.getByLabel('修改原因').fill('长'.repeat(201))
  await expect(dialog.getByText('修改原因最多 200 字')).toBeVisible()
  await expect(next).toBeDisabled()

  expect(overrideCalls(api), 'invalid drafts send nothing').toHaveLength(0)
})

test('invariant 5: an override confirms, sends six fields and shows the rebuilt plan', async ({
  page,
}) => {
  const api = await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)
  const preview = await openPreview(page, '合成学校甲')

  await preview.getByRole('button', { name: '写入覆盖' }).click()
  const dialog = page.getByRole('dialog', { name: '写入窗口覆盖？' })
  await dialog.getByLabel('开始时间').fill('2026-10-28T08:30')
  await dialog.getByLabel('结束时间').fill('2026-10-28T09:20')
  await dialog.getByLabel('修改原因').fill('合成调整窗口')
  await dialog.getByRole('button', { name: '下一步：确认覆盖' }).click()

  const confirm = page.getByRole('dialog', { name: '写入覆盖？' })
  await expect(confirm).toBeVisible()
  await expect(confirm).toContainText('pending 行将立即重排；running 与终态行不受影响')
  expect(overrideCalls(api), 'no write before confirmation').toHaveLength(0)

  await confirm.getByRole('button', { name: '确认覆盖' }).click()
  await expect.poll(() => overrideCalls(api).length).toBe(1)
  expect(overrideCalls(api)[0].body).toEqual({
    day: SCHEDULE_DAY,
    school_id: 'school-alpha',
    task_id: 'task-daily',
    start: '2026-10-28T08:30:00+08:00',
    end: '2026-10-28T09:20:00+08:00',
    reason: '合成调整窗口',
  })

  await expect(confirm).toBeHidden()
  await expect(dialog).toBeHidden()
  await expect(preview).toBeVisible()
  await expect(preview).toContainText('v3')
  await expect(preview).toContainText('08:20')
  await expect(preview).toContainText('09:20')
})

test('invariant 5: a 409 override reports the disabled task', async ({ page }) => {
  await mockScheduleApi(page, {
    role: 'master',
    overrideError: { status: 409, code: 'conflict', message: 'task disabled' },
  })
  await openSchedule(page)
  const preview = await openPreview(page, '合成学校甲')

  await preview.getByRole('button', { name: '写入覆盖' }).click()
  const dialog = page.getByRole('dialog', { name: '写入窗口覆盖？' })
  await dialog.getByLabel('开始时间').fill('2026-10-28T08:30')
  await dialog.getByLabel('结束时间').fill('2026-10-28T09:20')
  await dialog.getByLabel('修改原因').fill('合成调整窗口')
  await dialog.getByRole('button', { name: '下一步：确认覆盖' }).click()

  const confirm = page.getByRole('dialog', { name: '写入覆盖？' })
  await confirm.getByRole('button', { name: '确认覆盖' }).click()
  await expect(confirm.getByText('任务已停用')).toBeVisible()
})

test('invariant 6: managers see no time preference card', async ({ page }) => {
  const api = await mockScheduleApi(page, { role: 'master' })
  await page.goto('/')
  await expect(page.getByText('签到时间偏好')).toHaveCount(0)
  expect(timePrefGetCalls(api), 'the card is the only reader').toHaveLength(0)
})

test('invariant 7: the preview modal traps focus, closes on Escape and restores it', async ({
  page,
}) => {
  await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)

  const opener = page.getByRole('button', { name: '查看计划 合成学校甲' })
  await opener.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '计划预览' })
  await expect(dialog).toBeVisible()
  expect(await focusInDialog(page, '计划预览'), 'focus starts inside the preview').toBe(true)

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect.poll(() => activeLabel(page)).toBe('查看计划 合成学校甲')

  await opener.click()
  await expect(dialog).toBeVisible()
  await page.locator('.app-modal-backdrop').click({ position: { x: 8, y: 8 } })
  await expect(dialog).toBeHidden()
  await expect.poll(() => activeLabel(page)).toBe('查看计划 合成学校甲')
})

test('invariant 7: the override modal layers over the preview and closes alone', async ({
  page,
}) => {
  await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)
  const preview = await openPreview(page, '合成学校甲')

  const opener = preview.getByRole('button', { name: '写入覆盖' })
  await opener.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '写入窗口覆盖？' })
  await expect(dialog).toBeVisible()
  expect(await focusInDialog(page, '写入窗口覆盖'), 'focus starts inside the override').toBe(true)

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(preview, 'the preview stays open').toBeVisible()
  await expect.poll(() => activeText(page)).toBe('写入覆盖')

  await opener.click()
  await expect(dialog).toBeVisible()
  await page.locator('.app-modal-backdrop').last().click({ position: { x: 8, y: 8 } })
  await expect(dialog).toBeHidden()
  await expect(preview).toBeVisible()
  await expect.poll(() => activeText(page)).toBe('写入覆盖')
})

test('invariant 7: the confirm step is modal and Escape returns to the preview', async ({
  page,
}) => {
  await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)
  const preview = await openPreview(page, '合成学校甲')

  const opener = preview.getByRole('button', { name: '落库此计划' })
  await opener.focus()
  await page.keyboard.press('Enter')
  const confirm = page.getByRole('dialog', { name: '落库此计划？' })
  await expect(confirm).toBeVisible()
  expect(await focusInDialog(page, '落库此计划'), 'focus starts inside the confirm').toBe(true)

  await page.keyboard.press('Escape')
  await expect(confirm).toBeHidden()
  await expect(preview).toBeVisible()
  await expect.poll(() => activeText(page)).toBe('落库此计划')

  await opener.click()
  await expect(confirm).toBeVisible()
  await page.locator('.app-modal-backdrop').last().click({ position: { x: 8, y: 8 } })
  await expect(confirm).toBeHidden()
  await expect(preview).toBeVisible()
})

test('invariant 8: 390px viewport renders /system plans without horizontal overflow', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockScheduleApi(page, { role: 'master' })
  await openSchedule(page)

  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  expect(
    overflow.scrollWidth,
    `390px overflow: ${overflow.scrollWidth} > ${overflow.clientWidth}`,
  ).toBeLessThanOrEqual(overflow.clientWidth)
})
