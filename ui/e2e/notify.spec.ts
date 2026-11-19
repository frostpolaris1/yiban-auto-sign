import { test, expect, type Locator, type Page } from '@playwright/test'
import {
  createDefaultNotifications,
  markAllReadCalls,
  markReadCalls,
  mockNotifyApi,
  notificationListCalls,
  unreadCountCalls,
  type Notification,
} from './support/fake-notify-api'

/**
 * 顶栏通知收件箱的不变式：
 *
 * 1. 未读角标与列表呈数一致：未读 3 → 角标 3；点开抽屉列出条目，未读有底色，
 *    四类类别、相对时间与两行截断正常渲染，关闭后焦点回到铃铛。
 * 2. 单条已读：POST /read 恰好 1 次，角标 −1，该行未读底色消失。
 * 3. 全部已读：POST /read-all 恰好 1 次，角标隐藏，全部转为已读态，重复点击幂等。
 * 4. 游标分页：next_cursor 非空显示「加载更多」，翻页请求对应 cursor 且不重发同游标，末页按钮隐藏。
 * 5. 30 秒轮询：页面可见且抽屉关闭时轮询；抽屉打开暂停；页面隐藏暂停。
 * 6. 负向：列表失败保留角标并出错误与重试；单条已读失败留原位提示；空列表显示「暂无通知」；
 *    user 角色零铃铛、零通知请求。
 */

function bellButton(page: Page): Locator {
  return page.locator('.topbar-actions').getByRole('button', { name: /通知/ })
}

/** 模态抽屉打开期间 Radix 会给背景设 aria-hidden；用物理定位读取角标 DOM。
 * 2026-11-19 任务书 06 起顶栏有两个 dialog 触发器（搜索与铃铛），
 * 故选择器按铃铛的可见名「通知…」锚定，不再用 aria-haspopup 单选。 */
function bellBadgeDom(page: Page): Locator {
  return page.locator('.topbar-actions button[aria-label^="通知"] .count')
}

function inboxDrawer(page: Page): Locator {
  return page.getByRole('dialog', { name: '通知收件箱' })
}

test('invariant 1: unread badge 3 opens inbox drawer with unread surface, categories and focus return', async ({
  page,
}) => {
  const api = await mockNotifyApi(page, { role: 'master' })
  await page.goto('/design')

  const bell = bellButton(page)
  await expect(bell).toBeVisible()
  await expect(bell).toHaveAttribute('aria-expanded', 'false')
  await expect(bell.locator('.count')).toHaveText('3')
  expect(unreadCountCalls(api).length).toBeGreaterThanOrEqual(1)

  await bell.click()
  const drawer = inboxDrawer(page)
  await expect(drawer).toBeVisible()
  // 抽屉打开时 Radix 给背景设 aria-hidden，getByRole 够不着铃铛；
  // 原物理选择器靠 aria-haspopup 单选，任务书 06 后顶栏有两个 dialog 触发器，
  // 故改按铃铛的 aria-label 前缀「通知」锚定，语义不变（仍是物理读属性）。
  await expect(page.locator('.topbar-actions button[aria-label^="通知"]')).toHaveAttribute(
    'aria-expanded',
    'true',
  )

  const items = drawer.locator('.app-inbox-item')
  await expect(items).toHaveCount(4)

  // 未读与已读底色可区分
  const unreadItem = items.nth(0)
  const readItem = items.nth(3)
  await expect(unreadItem).toHaveAttribute('data-unread', 'true')
  await expect(readItem).toHaveAttribute('data-unread', 'false')
  const unreadBg = await unreadItem.evaluate((el) => getComputedStyle(el).backgroundColor)
  const readBg = await readItem.evaluate((el) => getComputedStyle(el).backgroundColor)
  expect(unreadBg).not.toBe(readBg)

  // 四类标签、相对时间、两行截断校验
  await expect(drawer).toContainText('凭据失效')
  await expect(drawer).toContainText('账号待审')
  await expect(drawer).toContainText('容量不足')
  await expect(drawer).toContainText('失败摘要')
  await expect(drawer).toContainText('5 分钟前')
  await expect(drawer).toContainText('昨天 18:20')

  const previewLineClamp = await unreadItem
    .locator('.app-inbox-preview')
    .evaluate((el) => getComputedStyle(el).webkitLineClamp)
  expect(previewLineClamp).toBe('2')

  // Esc 关闭并将焦点还给铃铛
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect(bell).toBeFocused()
})

test('invariant 2: single mark-read sends /read once, decrements badge and clears unread surface', async ({
  page,
}) => {
  const api = await mockNotifyApi(page, { role: 'master' })
  await page.goto('/design')

  const bell = bellButton(page)
  await expect(bell.locator('.count')).toHaveText('3')
  await bell.click()

  const drawer = inboxDrawer(page)
  const firstItem = drawer.locator('.app-inbox-item').first()
  await expect(firstItem).toHaveAttribute('data-unread', 'true')
  await firstItem.getByRole('button', { name: /标为已读/ }).click()

  await expect.poll(() => markReadCalls(api).length).toBe(1)
  expect(markReadCalls(api)[0].body).toEqual({
    ids: ['00000000-0000-4000-8000-000000000001'],
  })
  await expect(firstItem).toHaveAttribute('data-unread', 'false')
  await expect(firstItem.getByRole('button', { name: /标为已读/ })).toHaveCount(0)
  await expect(bellBadgeDom(page)).toHaveText('2')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect(bell.locator('.count')).toHaveText('2')
})

test('invariant 3: mark-all-read sends /read-all once, hides badge, marks all read and stays idempotent', async ({
  page,
}) => {
  const api = await mockNotifyApi(page, { role: 'master' })
  await page.goto('/design')

  const bell = bellButton(page)
  await expect(bell.locator('.count')).toHaveText('3')
  await bell.click()

  const drawer = inboxDrawer(page)
  const markAllBtn = drawer.getByRole('button', { name: '全部已读' })
  await markAllBtn.click()

  await expect.poll(() => markAllReadCalls(api).length).toBe(1)
  await expect(bellBadgeDom(page)).toHaveCount(0)
  await expect(drawer.locator('.app-inbox-item[data-unread="true"]')).toHaveCount(0)

  // 幂等再按一次仍正常 204
  await markAllBtn.click()
  await expect.poll(() => markAllReadCalls(api).length).toBe(2)
  await expect(drawer.getByRole('alert')).toHaveCount(0)

  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect(bell.locator('.count')).toHaveCount(0)
})

test('invariant 4: cursor pagination loads next page without duplicate cursor requests and hides load more at end', async ({
  page,
}) => {
  const defaults = createDefaultNotifications()
  const page1: Notification[] = defaults.slice(0, 2)
  const page2: Notification[] = defaults.slice(2, 4)

  const api = await mockNotifyApi(page, {
    role: 'admin',
    pages: { '': page1, 'cursor-2': page2 },
    nextCursors: { '': 'cursor-2', 'cursor-2': '' },
    listDelayMs: 120,
  })
  await page.goto('/design')
  await bellButton(page).click()

  const drawer = inboxDrawer(page)
  await expect(drawer.locator('.app-inbox-item')).toHaveCount(2)
  const loadMore = drawer.getByRole('button', { name: '加载更多' })
  await expect(loadMore).toBeVisible()

  // 快速连击只发一次 cursor-2 请求
  await loadMore.click()
  const busyBtn = drawer.getByRole('button', { name: '加载中…' })
  if ((await busyBtn.count()) > 0) {
    await busyBtn.click({ force: true })
  }

  await expect(drawer.locator('.app-inbox-item')).toHaveCount(4)
  await expect(loadMore).toHaveCount(0)

  const cursor2Calls = notificationListCalls(api).filter(
    (call) => call.params.cursor === 'cursor-2',
  )
  expect(cursor2Calls).toHaveLength(1)
  expect(notificationListCalls(api)[0].params.limit).toBe('20')
})

test('invariant 5: badge polls every 30s when visible, pauses while drawer is open and pauses when hidden', async ({
  page,
}) => {
  await page.clock.install()
  const api = await mockNotifyApi(page, { role: 'master' })
  await page.goto('/design')

  const bell = bellButton(page)
  await expect(bell.locator('.count')).toHaveText('3')
  const initialCalls = unreadCountCalls(api).length

  // 抽屉关闭时，30s 后自动轮询
  await page.clock.runFor(31_000)
  await expect.poll(() => unreadCountCalls(api).length).toBe(initialCalls + 1)

  // 抽屉打开时暂停轮询
  await bell.click()
  await expect(inboxDrawer(page)).toBeVisible()
  const whileOpenBase = unreadCountCalls(api).length
  await page.clock.runFor(65_000)
  expect(unreadCountCalls(api).length, 'no unread poll while drawer is open').toBe(whileOpenBase)

  // 关闭抽屉恢复，页面隐藏时同样暂停
  await page.keyboard.press('Escape')
  await expect(inboxDrawer(page)).toBeHidden()
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    window.dispatchEvent(new Event('visibilitychange'))
  })
  const whileHiddenBase = unreadCountCalls(api).length
  await page.clock.runFor(65_000)
  expect(unreadCountCalls(api).length, 'no unread poll while document is hidden').toBe(
    whileHiddenBase,
  )

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    window.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(31_000)
  await expect.poll(() => unreadCountCalls(api).length).toBe(whileHiddenBase + 1)
})

test('invariant 6a: list error keeps unread badge and offers retry; read error keeps unread item in place', async ({
  page,
}) => {
  const api = await mockNotifyApi(page, {
    role: 'master',
    listError: { status: 502, code: 'upstream_error', message: '通知服务暂不可用', once: true },
    readError: { status: 500, code: 'internal_error', message: '标记已读失败', once: true },
  })
  await page.goto('/design')

  const bell = bellButton(page)
  await expect(bell.locator('.count')).toHaveText('3')

  // 列表失败出错误＋重试，角标保留 3
  await bell.click()
  const drawer = inboxDrawer(page)
  await expect(drawer.getByRole('alert')).toContainText('通知服务暂不可用')
  await expect(bellBadgeDom(page)).toHaveText('3')

  await drawer.getByRole('button', { name: '重试' }).click()
  await expect(drawer.locator('.app-inbox-item')).toHaveCount(4)

  // 单条已读失败：保留未读态与角标，抽屉内提示失败
  const firstItem = drawer.locator('.app-inbox-item').first()
  await firstItem.getByRole('button', { name: /标为已读/ }).click()
  await expect.poll(() => markReadCalls(api).length).toBe(1)
  await expect(drawer.getByRole('alert')).toContainText('标记已读失败')
  await expect(firstItem).toHaveAttribute('data-unread', 'true')
  await expect(bellBadgeDom(page)).toHaveText('3')
})

test('invariant 6b: empty inbox shows status message rather than error', async ({ page }) => {
  await mockNotifyApi(page, { role: 'master', notifications: [] })
  await page.goto('/design')

  const bell = bellButton(page)
  await expect(bell.locator('.count')).toHaveCount(0)
  await bell.click()

  const drawer = inboxDrawer(page)
  await expect(drawer.getByRole('status')).toContainText('暂无通知')
  await expect(drawer.getByRole('alert')).toHaveCount(0)
})

test('invariant 6c: user role renders no bell and sends zero notification requests', async ({
  page,
}) => {
  const api = await mockNotifyApi(page, { role: 'user' })
  await page.goto('/design')

  await expect(page.getByRole('heading', { name: /组件画廊/ })).toBeVisible()
  await expect(bellButton(page)).toHaveCount(0)
  expect(unreadCountCalls(api)).toHaveLength(0)
  expect(notificationListCalls(api)).toHaveLength(0)
  expect(markReadCalls(api)).toHaveLength(0)
  expect(markAllReadCalls(api)).toHaveLength(0)
})

test('invariant 6d: next-page failure keeps page 1 and retries without duplicating cursor', async ({
  page,
}) => {
  const defaults = createDefaultNotifications()
  const page1: Notification[] = defaults.slice(0, 2)
  const page2: Notification[] = defaults.slice(2, 4)

  const api = await mockNotifyApi(page, {
    role: 'admin',
    pages: { '': page1, 'cursor-2': page2 },
    nextCursors: { '': 'cursor-2', 'cursor-2': '' },
  })
  await page.goto('/design')
  await bellButton(page).click()

  const drawer = inboxDrawer(page)
  await expect(drawer.locator('.app-inbox-item')).toHaveCount(2)

  api.setListError({ status: 502, code: 'upstream_error', message: '下一页加载失败', once: true })
  await drawer.getByRole('button', { name: '加载更多' }).click()

  await expect(drawer.locator('.app-inbox-item')).toHaveCount(2)
  await expect(drawer.getByRole('alert')).toContainText('下一页加载失败')

  await drawer.getByRole('button', { name: '重试' }).click()
  await expect(drawer.locator('.app-inbox-item')).toHaveCount(4)
  await expect(drawer.getByRole('alert')).toHaveCount(0)

  const cursor2Calls = notificationListCalls(api).filter(
    (call) => call.params.cursor === 'cursor-2',
  )
  expect(cursor2Calls).toHaveLength(2)
})

test('invariant 6e: read-all error preserves unread state and shows inline alert', async ({
  page,
}) => {
  const api = await mockNotifyApi(page, {
    role: 'master',
    readAllError: { status: 500, code: 'internal_error', message: '批量已读失败', once: true },
  })
  await page.goto('/design')

  await bellButton(page).click()
  const drawer = inboxDrawer(page)
  await drawer.getByRole('button', { name: '全部已读' }).click()

  await expect.poll(() => markAllReadCalls(api).length).toBe(1)
  await expect(drawer.getByRole('alert')).toContainText('批量已读失败')
  await expect(drawer.locator('.app-inbox-item[data-unread="true"]')).toHaveCount(3)
  await expect(bellBadgeDom(page)).toHaveText('3')
})

test('invariant 6f: re-open refetch error preserves cached items, keeps mark-all enabled and retries', async ({
  page,
}) => {
  const api = await mockNotifyApi(page, { role: 'master' })
  await page.goto('/design')

  const bell = bellButton(page)
  await bell.click()
  const drawer = inboxDrawer(page)
  await expect(drawer.locator('.app-inbox-item')).toHaveCount(4)

  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  api.setListError({ status: 503, code: 'unavailable', message: '刷新通知失败', once: true })
  await bell.click()
  await expect(drawer).toBeVisible()

  await expect(drawer.getByRole('alert')).toContainText('刷新通知失败')
  await expect(drawer.locator('.app-inbox-item')).toHaveCount(4)
  await expect(drawer.getByRole('button', { name: '全部已读' })).toBeEnabled()

  await drawer.getByRole('button', { name: '重试' }).click()
  await expect(drawer.getByRole('alert')).toHaveCount(0)
  await expect(drawer.locator('.app-inbox-item')).toHaveCount(4)
})

test('invariant 7: inbox drawer has zero horizontal overflow at 390px', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockNotifyApi(page, { role: 'master' })
  await page.goto('/design')

  await bellButton(page).click()
  const drawer = inboxDrawer(page)
  await expect(drawer).toBeVisible()
  await expect(drawer.locator('.app-inbox-item')).toHaveCount(4)

  const metrics = await drawer.evaluate((el) => ({
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
  }))
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1)

  const doc = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth + 1)
})
