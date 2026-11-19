/**
 * `/system` 运行策略（#policy）、全站公告（#announcement）与渠道测试（#channels）行为守卫。
 *
 * 断言用户可见不变式：
 * 1. 分区锚点：页顶锚链含「计划 / 策略 / 公告 / 渠道」；点击锚链跳转对应分区并设 aria-current。
 * 2. 4 张策略卡显式保存：切换开关本身不发请求，点击各卡独立保存才发出精准契约负载并弹出成功提示；
 *    调度策略卡读写全部 8 键并在保存时同步更新；配额卡与调度卡支持 null 恢复默认；
 *    负例（配额越界、边届缩进越界、默认窗口 start>=end）在提交前拦截并聚焦首个错误控件。
 * 3. 全站公告：通过 `GET /api/announcement` 回显正文与更新时间（零依赖 `GET /api/admin/settings`）；
 *    拦截多行与超 200 字提交；支持发布与 `{ text: "" }` 清空。
 * 4. 渠道测试：测试邮件弹窗收集 `current_password`，`400/403` 与 reauth 节流 `429` 留在弹窗内，
 *    `409` / 冷却 `429`（含 Retry-After 秒数） / `502` 关闭弹窗并在按钮旁展示；
 *    Webhook 无弹窗直发空 POST 并内联展示结果。
 * 5. RBAC 与窄屏：`admin` 只读看到全部读数但所有写按钮置灰并标出原因且零写请求；
 *    `user` 仅渲染无权卡片、零管理读请求；390px 视口无水平溢出。
 */

import { expect, test, type Locator, type Page } from '@playwright/test'
import { SCHEDULE_DAY, mockScheduleApi, type ScheduleApiCall } from './support/fake-schedule-api'

async function visitSystem(
  page: Page,
  options: Parameters<typeof mockScheduleApi>[1] = { role: 'master' },
) {
  const api = await mockScheduleApi(page, options)
  await page.goto(`/system?day=${SCHEDULE_DAY}`)
  await expect(page.getByRole('heading', { level: 1 })).toContainText('系统设置')
  return api
}

function callsTo(calls: ScheduleApiCall[], method: string, path: string): ScheduleApiCall[] {
  return calls.filter((call) => call.method === method && call.path === path)
}

test('system anchors render all 4 sections and navigate via hash', async ({ page }) => {
  await visitSystem(page, { role: 'master' })

  const nav = page.getByRole('navigation', { name: '系统设置分区' })
  for (const label of ['计划', '策略', '公告', '渠道']) {
    await expect(nav.getByRole('link', { name: label, exact: true })).toBeVisible()
  }

  await nav.getByRole('link', { name: '策略', exact: true }).click()
  await expect(nav.getByRole('link', { name: '策略', exact: true })).toHaveAttribute(
    'aria-current',
    'location',
  )
  await expect(page.locator('#policy')).toBeFocused()

  await nav.getByRole('link', { name: '公告', exact: true }).click()
  await expect(nav.getByRole('link', { name: '公告', exact: true })).toHaveAttribute(
    'aria-current',
    'location',
  )

  await nav.getByRole('link', { name: '渠道', exact: true }).click()
  await expect(nav.getByRole('link', { name: '渠道', exact: true })).toHaveAttribute(
    'aria-current',
    'location',
  )

  // 滚动至页底后「渠道」仍必须维持激活，不被上方的公告交集抢占。
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
  await page.waitForTimeout(950)
  await expect(nav.getByRole('link', { name: '渠道', exact: true })).toHaveAttribute(
    'aria-current',
    'location',
  )
})

test('registration and auto-approve cards save explicitly on button click only', async ({ page }) => {
  const api = await visitSystem(page, {
    role: 'master',
    registration: { enabled: false, terms_version: 1, privacy_version: 1 },
    autoApprove: { enabled: true },
  })

  const regCard = page.locator('#policy-registration-card')
  const regSwitch = regCard.getByRole('switch', { name: '允许新用户公开注册' })
  await expect(regSwitch).toHaveAttribute('aria-checked', 'false')

  // 单点切换开关不得立即触发 PUT。
  await regSwitch.click()
  await expect(regSwitch).toHaveAttribute('aria-checked', 'true')
  expect(callsTo(api.calls, 'PUT', '/api/auth/registration')).toEqual([])

  await regCard.getByRole('button', { name: '保存设置' }).click()
  await expect(page.getByText('已开放公开注册')).toBeVisible()
  expect(callsTo(api.calls, 'PUT', '/api/auth/registration').map((c) => c.body)).toEqual([
    { enabled: true },
  ])

  const approveCard = page.locator('#policy-auto-approve-card')
  const approveSwitch = approveCard.getByRole('switch', { name: '开启新号自动审核' })
  await expect(approveSwitch).toHaveAttribute('aria-checked', 'true')
  await approveSwitch.click()
  expect(callsTo(api.calls, 'PUT', '/api/admin/auto-approve')).toEqual([])

  await approveCard.getByRole('button', { name: '保存设置' }).click()
  await expect(page.getByText('已关闭自动审核')).toBeVisible()
  expect(callsTo(api.calls, 'PUT', '/api/admin/auto-approve').map((c) => c.body)).toEqual([
    { enabled: false },
  ])

  // 刷新后读到后端最新状态。
  await page.reload()
  await expect(regCard.getByRole('switch', { name: '允许新用户公开注册' })).toHaveAttribute(
    'aria-checked',
    'true',
  )
  await expect(approveCard.getByRole('switch', { name: '开启新号自动审核' })).toHaveAttribute(
    'aria-checked',
    'false',
  )
})

test('quota card validates bounds client-side, saves unlimited 0, and resets to default 1', async ({
  page,
}) => {
  const api = await visitSystem(page, {
    role: 'master',
    quota: {
      max_users: { value: 1, source: 'default' },
      max_accounts: { value: 1, source: 'default' },
    },
  })

  const card = page.locator('#policy-quota-card')
  const usersInput = card.getByLabel('全站活跃用户上限')
  const accountsInput = card.getByLabel('全站启用账号上限')

  await expect(card.getByRole('button', { name: '恢复默认 (1)' })).toBeDisabled()

  // 负例：负值、超上限、非整数都在提交前拦截并聚焦首个错框，零写入。
  await usersInput.fill('-1')
  await accountsInput.fill('100001')
  await card.getByRole('button', { name: '保存配额' }).click()
  await expect(card).toContainText('请输入 0 至 100000 之间的整数')
  await expect(usersInput).toHaveAttribute('aria-invalid', 'true')
  await expect(usersInput).toBeFocused()
  expect(callsTo(api.calls, 'PUT', '/api/admin/quota-settings')).toEqual([])

  // 合法提交：max_users=0（不限），max_accounts=25。
  await usersInput.fill('0')
  await accountsInput.fill('25')
  await card.getByRole('button', { name: '保存配额' }).click()
  await expect(page.getByText('容量配额已保存')).toBeVisible()
  expect(callsTo(api.calls, 'PUT', '/api/admin/quota-settings').map((c) => c.body)).toEqual([
    { max_users: 0, max_accounts: 25 },
  ])
  await expect(card).toContainText('数据库覆盖')

  // 显式 null 恢复默认 1。
  const resetBtn = card.getByRole('button', { name: '恢复默认 (1)' })
  await expect(resetBtn).toBeEnabled()
  await resetBtn.click()
  await expect(usersInput).toHaveValue('1')
  await expect(accountsInput).toHaveValue('1')
  expect(callsTo(api.calls, 'PUT', '/api/admin/quota-settings').at(-1)?.body).toEqual({
    max_users: null,
    max_accounts: null,
  })
})

test('schedule policy card reads all 8 keys, rejects invalid inputs, saves patch, and resets', async ({
  page,
}) => {
  const api = await visitSystem(page, { role: 'master' })
  const card = page.locator('#policy-schedule-card')

  const frontInput = card.getByLabel('前边界缩进（秒）')
  const backInput = card.getByLabel('后边界缩进（秒）')
  const startInput = card.getByLabel('默认开始时间（上海时间）')
  const endInput = card.getByLabel('默认结束时间（上海时间）')

  await expect(frontInput).toHaveValue('60')
  await expect(startInput).toHaveValue('06:30')
  await expect(endInput).toHaveValue('07:50')

  // 负例 1：边界秒超域。
  await frontInput.fill('301')
  await card.getByRole('button', { name: '保存调度策略' }).click()
  await expect(card).toContainText('请输入 0 至 300 之间的整数秒')
  await expect(frontInput).toBeFocused()
  expect(callsTo(api.calls, 'PUT', '/api/admin/schedule-settings')).toEqual([])

  // 负例 2：默认结束时间早于等于开始时间。
  await frontInput.fill('30')
  await startInput.fill('08:30')
  await endInput.fill('08:30')
  await card.getByRole('button', { name: '保存调度策略' }).click()
  await expect(card).toContainText('结束时间必须晚于开始时间')
  await expect(endInput).toBeFocused()
  expect(callsTo(api.calls, 'PUT', '/api/admin/schedule-settings')).toEqual([])

  // 正例：一次改全 8 个键（edge/偏好/周末门/默认窗口/分布档）。
  await backInput.fill('90')
  await card.getByRole('switch', { name: '允许用户自选签到时段' }).click()
  await card.getByRole('switch', { name: '周六开启签到' }).click()
  await card.getByRole('switch', { name: '周日开启签到' }).click()
  await startInput.fill('07:00')
  await endInput.fill('08:30')
  await card.getByRole('radio', { name: /全窗均匀分散/ }).check()

  await card.getByRole('button', { name: '保存调度策略' }).click()
  await expect(page.getByText('调度运行策略已保存')).toBeVisible()

  expect(callsTo(api.calls, 'PUT', '/api/admin/schedule-settings').map((c) => c.body)).toEqual([
    {
      edge_front_sec: 30,
      edge_back_sec: 90,
      allow_time_pref: true,
      saturday_sign: true,
      sunday_sign: true,
      default_sign_start_min: 420,
      default_sign_end_min: 510,
      sign_dist: 'uniform',
    },
  ])
  await expect(card).toContainText('数据库覆盖')

  // 恢复全部 8 键默认。
  await card.getByRole('button', { name: '恢复调度默认' }).click()
  await expect(frontInput).toHaveValue('60')
  await expect(startInput).toHaveValue('06:30')
  await expect(endInput).toHaveValue('07:50')
  await expect(card.getByRole('radio', { name: /前段顺序填充/ })).toBeChecked()
  expect(callsTo(api.calls, 'PUT', '/api/admin/schedule-settings').at(-1)?.body).toEqual({
    edge_front_sec: null,
    edge_back_sec: null,
    allow_time_pref: null,
    sunday_sign: null,
    saturday_sign: null,
    default_sign_start_min: null,
    default_sign_end_min: null,
    sign_dist: null,
  })
})

test('announcement reads GET /api/announcement, rejects line breaks and >200 chars, publishes and clears', async ({
  page,
}) => {
  const api = await visitSystem(page, {
    role: 'master',
    announcement: { text: '初始维护公告', updated_at: '2026-11-18T10:00:00Z' },
  })
  const section = page.locator('#announcement')
  const input = section.getByLabel('公告正文')

  await expect(section).toContainText('初始维护公告')
  await expect(section).toContainText('2026/11/18')
  expect(callsTo(api.calls, 'GET', '/api/announcement').length).toBeGreaterThan(0)
  expect(callsTo(api.calls, 'GET', '/api/admin/settings')).toEqual([])

  // 负例：换行与超 200 字都拦在请求之前。
  await input.fill('第一行\n第二行')
  await section.getByRole('button', { name: '发布公告' }).click()
  await expect(section).toContainText('不能包含换行')
  expect(callsTo(api.calls, 'PUT', '/api/admin/announcement')).toEqual([])

  await input.fill('公'.repeat(201))
  await section.getByRole('button', { name: '发布公告' }).click()
  await expect(section).toContainText('最多 200 个字符')
  expect(callsTo(api.calls, 'PUT', '/api/admin/announcement')).toEqual([])

  // 发布新公告并裁剪空白。
  await input.fill('  明早 06:00 正常签退，请提前核对密码。  ')
  await section.getByRole('button', { name: '发布公告' }).click()
  await expect(page.getByText('全站公告已发布')).toBeVisible()
  await expect(section).toContainText('明早 06:00 正常签退，请提前核对密码。')
  expect(callsTo(api.calls, 'PUT', '/api/admin/announcement').map((c) => c.body)).toEqual([
    { text: '明早 06:00 正常签退，请提前核对密码。' },
  ])

  // 清空公告。
  await section.getByRole('button', { name: '清空公告' }).click()
  await expect(page.getByText('全站公告已清空')).toBeVisible()
  await expect(section).toContainText('当前未发布全站公告')
  await expect(section).toContainText('未发布')
  await expect(section.getByRole('button', { name: '清空公告' })).toBeDisabled()
  expect(callsTo(api.calls, 'PUT', '/api/admin/announcement').at(-1)?.body).toEqual({ text: '' })
})

test('mail test modal isolates reauth failures (400/403/429-reauth) inside dialog and surfaces 409/429-cooldown/502/200 beside button', async ({
  page,
}) => {
  const queue = [
    { status: 400, code: 'validation', message: 'current_password is invalid' },
    { status: 403, code: 'forbidden', message: 'current password is incorrect' },
    { status: 429, code: 'rate_limited', message: 'too many failed reauth attempts', retryAfterSec: 90 },
    { status: 409, code: 'conflict', message: 'mail delivery is disabled' },
    {
      status: 429,
      code: 'rate_limited',
      message: 'a test email was attempted recently; retry later',
      details: { retry_after_seconds: 45 },
      retryAfterSec: 45,
    },
    { status: 502, code: 'bad_gateway', message: 'smtp connection refused' },
    undefined,
  ]

  const api = await visitSystem(page, {
    role: 'master',
    onMailTest: () => queue.shift(),
  })

  const section = page.locator('#channels')
  const trigger = section.getByRole('button', { name: '测试邮件' })
  await trigger.click()

  const dialog = page.getByRole('dialog', { name: '确认发送测试邮件' })
  await expect(dialog).toBeVisible()
  const pwd = dialog.getByLabel(/^当前登录密码/)

  // 空口令前置拦截。
  await dialog.getByRole('button', { name: '确认发送' }).click()
  await expect(dialog).toContainText('请输入当前登录密码')
  expect(callsTo(api.calls, 'POST', '/api/admin/mail/test')).toEqual([])

  // 400 / 403 / reauth 429 留在弹窗内；输入空值二次提交须清除旧服务错误，不并存矛盾提示。
  for (const expectSnippet of ['请输入当前登录密码', '当前登录密码不正确', '90 秒后重试']) {
    await pwd.fill('wrong-pw')
    await dialog.getByRole('button', { name: '确认发送' }).click()
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText(expectSnippet)
  }

  await pwd.fill('')
  await dialog.getByRole('button', { name: '确认发送' }).click()
  await expect(dialog).toContainText('请输入当前登录密码')
  await expect(dialog).not.toContainText('密码复核尝试过多')

  // 取消与 Escape 关闭均恢复焦点至触发按钮。
  await dialog.getByRole('button', { name: '取消' }).click()
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()
  await trigger.click()
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()

  // 409 / cooldown 429 / 502 关闭弹窗并显示在按钮旁。
  for (const expectSnippet of ['SMTP 未开启', '45 秒', '投递失败']) {
    if (!(await dialog.isVisible())) {
      await trigger.click()
      await expect(dialog).toBeVisible()
    }
    await pwd.fill('master-pw')
    await dialog.getByRole('button', { name: '确认发送' }).click()
    await expect(dialog).toBeHidden()
    await expect(section).toContainText(expectSnippet)
  }

  // 200 成功。
  await trigger.click()
  await expect(dialog).toBeVisible()
  await pwd.fill('master-pw')
  await dialog.getByRole('button', { name: '确认发送' }).click()
  await expect(dialog).toBeHidden()
  await expect(section).toContainText('测试邮件已接受投递')
  await expect(page.getByText('测试邮件发送成功')).toBeVisible()
  await expect(trigger).toBeFocused()
})

test('mail test CSRF 403 closes reauth modal and shows CSRF hint beside button', async ({
  page,
}) => {
  await visitSystem(page, {
    role: 'master',
    onMailTest: () => ({
      status: 403,
      code: 'forbidden',
      message: 'missing or invalid CSRF token',
    }),
  })
  const section = page.locator('#channels')
  await section.getByRole('button', { name: '测试邮件' }).click()
  const dialog = page.getByRole('dialog', { name: '确认发送测试邮件' })
  await dialog.getByLabel(/^当前登录密码/).fill('master-pw')
  await dialog.getByRole('button', { name: '确认发送' }).click()
  await expect(dialog).toBeHidden()
  await expect(section).toContainText('CSRF')
})

test('webhook test sends empty POST without modal and renders 409/429/502/200 beside button', async ({
  page,
}) => {
  const queue = [
    { status: 409, code: 'conflict', message: 'webhook is disabled' },
    {
      status: 429,
      code: 'rate_limited',
      message: 'webhook cooldown active',
      details: { retry_after_seconds: 30 },
      retryAfterSec: 30,
    },
    { status: 502, code: 'bad_gateway', message: 'endpoint error' },
    undefined,
  ]

  const api = await visitSystem(page, {
    role: 'master',
    onWebhookTest: () => queue.shift(),
  })

  const section = page.locator('#channels')
  const trigger = section.getByRole('button', { name: '测试 Webhook' })

  for (const expectSnippet of ['推送开关未开启', '30 秒', '投递失败', '测试 Webhook 已发送']) {
    await trigger.click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(section).toContainText(expectSnippet)
  }

  const posts = callsTo(api.calls, 'POST', '/api/admin/webhook/test')
  expect(posts).toHaveLength(4)
  for (const call of posts) {
    expect(call.body).toBeNull()
  }
})

test('admin sees read-only disabled controls with reason and triggers zero writes', async ({ page }) => {
  const api = await visitSystem(page, { role: 'admin' })

  await expect(page.locator('#policy-registration-card')).toContainText('允许新用户公开注册')
  await expect(page.locator('#policy-auto-approve-card')).toContainText('开启新号自动审核')
  await expect(page.locator('#policy')).toContainText('全站活跃用户上限')
  await expect(page.locator('#announcement')).toContainText('当前线上公告')

  const disabledButtons: Locator[] = [
    page.locator('#policy-registration-card').getByRole('button', { name: '保存设置' }),
    page.locator('#policy-auto-approve-card').getByRole('button', { name: '保存设置' }),
    page.locator('#policy-quota-card').getByRole('button', { name: '保存配额' }),
    page.locator('#policy-schedule-card').getByRole('button', { name: '保存调度策略' }),
    page.locator('#announcement').getByRole('button', { name: '发布公告' }),
    page.locator('#announcement').getByRole('button', { name: '清空公告' }),
    page.locator('#channels').getByRole('button', { name: '测试邮件' }),
    page.locator('#channels').getByRole('button', { name: '测试 Webhook' }),
  ]

  for (const button of disabledButtons) {
    await expect(button).toBeDisabled()
    await button.click({ force: true })
  }

  // 禁用主按钮必须视觉降级（照 app.css 既有 0.45 档，防止假可点）。
  await expect(page.locator('#policy-quota-card').getByRole('button', { name: '保存配额' })).toHaveCSS(
    'opacity',
    '0.45',
  )

  for (const control of [
    page.locator('#policy-registration-card').getByRole('switch'),
    page.locator('#policy-auto-approve-card').getByRole('switch'),
    page.locator('#policy-quota-card').getByLabel('全站活跃用户上限'),
    page.locator('#policy-quota-card').getByLabel('全站启用账号上限'),
    page.locator('#policy-schedule-card').getByLabel('前边界缩进（秒）'),
    page.locator('#policy-schedule-card').getByRole('radio', { name: /front/ }),
    page.locator('#policy-schedule-card').getByRole('radio', { name: /uniform/ }),
    page.locator('#announcement').getByLabel('公告正文'),
  ]) {
    await expect(control).toBeDisabled()
  }

  for (const selector of ['#policy', '#announcement', '#channels']) {
    await expect(page.locator(selector).locator('.app-deny-note').first()).toContainText(
      '仅主管理员可修改全站运行策略',
    )
  }

  const writes = api.calls.filter((call) => call.method === 'PUT' || call.method === 'POST')
  expect(writes).toEqual([])
})

test('server write errors on policy and announcement display inline alerts and preserve draft', async ({
  page,
}) => {
  await visitSystem(page, {
    role: 'master',
    onPolicyWrite: (_method, path) =>
      path === '/api/admin/quota-settings'
        ? { status: 500, code: 'internal_error', message: 'db write failed' }
        : { status: 403, code: 'forbidden', message: 'master role is required' },
  })

  const quotaCard = page.locator('#policy-quota-card')
  await quotaCard.getByLabel('全站活跃用户上限').fill('88')
  await quotaCard.getByRole('button', { name: '保存配额' }).click()
  await expect(quotaCard.getByRole('alert')).toContainText('保存失败，请稍后重试')
  await expect(quotaCard.getByRole('alert')).not.toContainText('internal_error')
  await expect(quotaCard.getByLabel('全站活跃用户上限')).toHaveValue('88')

  const annSection = page.locator('#announcement')
  await annSection.getByLabel('公告正文').fill('保留草稿不被清空')
  await annSection.getByRole('button', { name: '发布公告' }).click()
  await expect(annSection.getByRole('alert')).toContainText('仅主管理员可发布或清空公告')
  await expect(annSection.getByLabel('公告正文')).toHaveValue('保留草稿不被清空')
})

test('user sees DeniedCard on /system and fires zero admin/policy/announcement requests', async ({
  page,
}) => {
  const api = await mockScheduleApi(page, { role: 'user' })
  await page.goto(`/system?day=${SCHEDULE_DAY}`)
  await expect(page.getByText('你的角色无权查看系统设置')).toBeVisible()
  await expect(page.locator('#policy')).toHaveCount(0)

  const leaked = api.calls.filter((call) => call.path !== '/api/auth/me')
  expect(leaked).toEqual([])
})

test('390px mobile viewport on /system has zero horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await visitSystem(page, { role: 'master' })
  await expect(page.locator('#policy-registration-card')).toContainText('允许新用户公开注册')
  await expect(page.locator('#policy-auto-approve-card')).toContainText('开启新号自动审核')
  await expect(page.locator('#policy-quota-card')).toContainText('全站活跃用户上限')
  await expect(page.locator('#policy-schedule-card')).toContainText('前边界缩进')
  await expect(page.locator('#announcement')).toContainText('当前线上公告')
  await expect(page.locator('#channels')).toContainText('测试邮件')

  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth)
})
