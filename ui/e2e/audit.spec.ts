import { test, expect, type Page } from '@playwright/test'
import {
  AUDIT_LOGS,
  AUDIT_STATUS_BROKEN,
  auditListCalls,
  auditStatusCalls,
  mockAuditApi,
} from './support/fake-audit-api'

/**
 * 审计日志页（/audit）的行为不变式，对应任务书 6 条验收谓词：
 *
 * 1. 默认第一页，`limit` 与筛选按契约发送；改筛选后 `cursor` 清空。
 * 2. 翻页只用 `cursor`；`next_cursor` 为空时下一页禁用。
 * 3. 断链态页头出危险告警且列表仍可用；列表失败只有列表区出错＋重试，页头结论保持。
 * 4. 无记录与筛选无命中两种空态文案不同；非法日期在客户端阻止提交并提示。
 * 5. user 直达 `/audit` 得无权限卡，且零审计请求。
 * 6. 390 筛选纵向堆叠、表格在自身容器内横向滚动、页面零横向溢出。
 */

async function openAudit(page: Page) {
  await expect(page.getByRole('heading', { level: 1, name: /审计\s*日志/ })).toBeVisible()
  await expect(page.locator('[data-audit-row]').first()).toBeVisible()
}

test('predicate 1: default page sends contract params, and a filter change clears the cursor', async ({
  page,
}) => {
  const api = await mockAuditApi(page, {
    role: 'master',
    pages: {
      '': AUDIT_LOGS.slice(0, 3),
      'cursor-2': AUDIT_LOGS.slice(3, 6),
    },
    nextCursors: { '': 'cursor-2' },
  })

  await page.goto('/audit')
  await openAudit(page)

  const first = auditListCalls(api)[0]
  expect(first.params.limit).toBe('20')
  expect(first.params.cursor).toBeUndefined()
  expect(first.params.action).toBeUndefined()
  expect(first.params.actor).toBeUndefined()
  expect(first.params.from).toBeUndefined()
  expect(first.params.to).toBeUndefined()

  // 翻到第二页后再改筛选：新请求不得带旧游标。
  await page.getByRole('button', { name: '下一页' }).click()
  await expect.poll(() => auditListCalls(api).at(-1)?.params.cursor).toBe('cursor-2')
  await expect(page.locator('[data-audit-pager]')).toContainText('第 2 页 · 本页 3 条')

  await page.getByLabel('操作者筛选').fill('m***@example.com')
  await expect
    .poll(() => auditListCalls(api).at(-1)?.params.actor)
    .toBe('m***@example.com')
  const afterFilter = auditListCalls(api).at(-1)
  expect(afterFilter?.params.cursor).toBeUndefined()
  expect(afterFilter?.params.limit).toBe('20')
  await expect(page.locator('[data-audit-pager]')).toContainText('第 1 页 · 本页 3 条')
})

test('predicate 2: paging only sends a cursor, and next is disabled on an empty next_cursor', async ({
  page,
}) => {
  const api = await mockAuditApi(page, {
    role: 'master',
    pages: {
      '': AUDIT_LOGS.slice(0, 2),
      'cursor-page-2': AUDIT_LOGS.slice(2, 4),
    },
    nextCursors: { '': 'cursor-page-2' },
  })

  await page.goto('/audit')
  await openAudit(page)
  await expect(page.locator('[data-audit-row]')).toHaveCount(2)

  await page.getByRole('button', { name: '下一页' }).click()
  await expect(page.locator('[data-audit-row]').first()).toHaveAttribute(
    'data-audit-id',
    String(AUDIT_LOGS[2].id),
  )

  const calls = auditListCalls(api)
  expect(calls.map((call) => call.params.cursor ?? '')).toEqual(['', 'cursor-page-2'])
  expect(calls.every((call) => call.params.limit === '20')).toBe(true)
  expect(calls.some((call) => 'offset' in call.params || 'page' in call.params)).toBe(false)

  // 第二页 next_cursor 为空：下一页禁用，上一页可用。
  await expect(page.getByRole('button', { name: '下一页' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '上一页' })).toBeEnabled()

  await page.getByRole('button', { name: '上一页' }).click()
  await expect.poll(() => auditListCalls(api).at(-1)?.params.cursor ?? '').toBe('')
  await expect(page.locator('[data-audit-row]').first()).toHaveAttribute(
    'data-audit-id',
    String(AUDIT_LOGS[0].id),
  )
})

test('predicate 2b: an empty cursor page shows the third empty state and returns to the first page', async ({
  page,
}) => {
  const api = await mockAuditApi(page, {
    role: 'master',
    pages: {
      '': AUDIT_LOGS.slice(0, 2),
      'cursor-page-2': [],
    },
    nextCursors: { '': 'cursor-page-2' },
  })

  await page.goto('/audit')
  await openAudit(page)
  await page.getByRole('button', { name: '下一页' }).click()

  await expect(page.getByText('本页无记录，可能已被删除')).toBeVisible()
  await expect(page.getByText('暂无审计记录')).toHaveCount(0)
  await expect(page.getByText('当前筛选无匹配记录')).toHaveCount(0)

  await page.getByRole('button', { name: '回第一页' }).click()
  await expect(page.locator('[data-audit-row]')).toHaveCount(2)
  await expect(page.locator('[data-audit-pager]')).toContainText('第 1 页 · 本页 2 条')
  await expect.poll(() => auditListCalls(api).at(-1)?.params.cursor ?? '').toBe('')
})

test('predicate 3: a broken chain warns in the header while rows stay readable; a list failure stays local', async ({
  page,
}) => {
  await mockAuditApi(page, { role: 'master', status: AUDIT_STATUS_BROKEN })
  await page.goto('/audit')
  await openAudit(page)

  const verdict = page.locator('[data-audit-verdict]')
  await expect(verdict).toHaveAttribute('data-audit-verdict', 'broken')
  await expect(verdict).toContainText('链异常')
  const chainAlert = page.getByRole('alert').filter({ hasText: '审计链异常' })
  await expect(chainAlert).toBeVisible()
  await expect(chainAlert).toContainText('第 4 行')
  await expect(page.locator('[data-audit-row]')).toHaveCount(6)

  // 列表失败：错误与重试只在列表区；页头结论保持完好。
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await mockAuditApi(page, {
    role: 'master',
    listError: {
      status: 500,
      code: 'internal_error',
      message: '审计列表暂不可用',
      once: true,
    },
  })
  await page.goto('/audit')

  const listCard = page.locator('.a-card')
  await expect(verdict).toHaveAttribute('data-audit-verdict', 'ok')
  await expect(verdict).toContainText('链完好')
  const listAlert = listCard.getByRole('alert')
  await expect(listAlert).toContainText('审计列表暂不可用')
  await expect(listAlert).not.toContainText('internal_error')
  await expect(listCard.locator('.a-state')).toHaveAttribute('title', '错误码：internal_error')
  await expect(page.locator('[data-audit-row]')).toHaveCount(0)

  await listCard.getByRole('button', { name: '重试' }).click()
  await expect(page.locator('[data-audit-row]')).toHaveCount(6)
  await expect(verdict).toContainText('链完好')
})

test('predicate 4: empty copy differs from filtered no-match, and an invalid date range is blocked', async ({
  page,
}) => {
  const api = await mockAuditApi(page, { role: 'master', logs: [] })
  await page.goto('/audit')

  // 无记录：不是「筛选无命中」。
  await expect(page.getByText('暂无审计记录')).toBeVisible()
  await expect(page.getByText('当前筛选无匹配记录')).toHaveCount(0)

  // 有筛选无命中：文案切换，且只留工具条一个清除入口。
  await page.getByLabel('操作者筛选').fill('nobody@example.com')
  await expect
    .poll(() => auditListCalls(api).at(-1)?.params.actor)
    .toBe('nobody@example.com')
  await expect(page.getByText('当前筛选无匹配记录')).toBeVisible()
  await expect(page.getByText('暂无审计记录')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '清除筛选' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: '清空筛选' })).toHaveCount(0)

  // 成对提交：两个日期都合法后才发一次查询。
  const fromInput = page.getByLabel('起始日期')
  const toInput = page.getByLabel('结束日期')
  const callsBeforeRange = auditListCalls(api).length
  await fromInput.fill('2026-11-10')
  await toInput.fill('2026-11-11')
  await expect
    .poll(() => auditListCalls(api).at(-1)?.params.to)
    .toBe('2026-11-11T23:59:59+08:00')
  expect(auditListCalls(api).length).toBe(callsBeforeRange + 1)
  expect(auditListCalls(api).at(-1)?.params.from).toBe('2026-11-10T00:00:00+08:00')

  // 终点改到起点之前：客户端阻止，保留两边草稿值，只标出错字段。
  const callsBeforeInvalid = auditListCalls(api).length
  await toInput.fill('2026-11-01')
  await expect(page.getByText('起始日期不能晚于结束日期')).toBeVisible()
  await expect(fromInput).toHaveValue('2026-11-10')
  await expect(toInput).toHaveValue('2026-11-01')
  await expect(toInput).toHaveAttribute('aria-invalid', 'true')
  await expect(fromInput).not.toHaveAttribute('aria-invalid', 'true')
  await expect(toInput).toHaveAttribute('aria-describedby', 'audit-filter-to-error')
  await expect(page.getByText('日期范围有误')).toBeVisible()
  await expect(page.getByText('当前筛选无匹配记录')).toHaveCount(0)

  await page.waitForTimeout(600)
  expect(auditListCalls(api).length).toBe(callsBeforeInvalid)
  expect(
    auditListCalls(api).some((call) => call.params.to === '2026-11-01T23:59:59+08:00'),
  ).toBe(false)
})

test('predicate 5: user reaches the denied card with zero audit requests', async ({ page }) => {
  const api = await mockAuditApi(page, { role: 'user' })
  await page.goto('/audit')

  await expect(page.getByText('无权访问')).toBeVisible()
  await expect(page.getByText('你的角色无权查看审计日志')).toBeVisible()
  expect(auditListCalls(api)).toHaveLength(0)
  expect(auditStatusCalls(api)).toHaveLength(0)
})

test('predicate 6: 390 stacks filters, keeps detail reachable, restores focus, and never overflows the page', async ({
  page,
}) => {
  const longDetail = JSON.stringify({ note: 'x'.repeat(2000) })
  const firstPage = [
    { ...AUDIT_LOGS[0], detail: longDetail },
    ...AUDIT_LOGS.slice(1, 3),
  ]
  await mockAuditApi(page, {
    role: 'master',
    pages: {
      '': firstPage,
      'cursor-page-2': AUDIT_LOGS.slice(3, 6),
    },
    nextCursors: { '': 'cursor-page-2' },
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/audit')
  await openAudit(page)

  const fromBox = await page.getByLabel('起始日期').boundingBox()
  const toBox = await page.getByLabel('结束日期').boundingBox()
  expect(fromBox).not.toBeNull()
  expect(toBox).not.toBeNull()
  expect(toBox!.y).toBeGreaterThan(fromBox!.y + fromBox!.height - 1)

  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1)

  const scroller = await page.locator('.a-table-scroll').evaluate((element) => ({
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
    overflowX: getComputedStyle(element).overflowX,
  }))
  expect(scroller.overflowX).toBe('auto')
  expect(scroller.scrollWidth).toBeGreaterThan(scroller.clientWidth)

  // 详情按钮在首屏视口内：390 下不横滚也够得着。
  const detailButton = page.getByRole('button', { name: `查看详情 #${AUDIT_LOGS[0].id}` })
  const buttonBox = await detailButton.boundingBox()
  expect(buttonBox).not.toBeNull()
  expect(buttonBox!.x).toBeGreaterThanOrEqual(0)
  expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(390)

  const drawer = page.getByRole('dialog', { name: '审计详情' })

  // Esc 关闭：焦点回到触发的详情按钮。
  await detailButton.click()
  await expect(drawer).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect(detailButton).toBeFocused()

  // 点 X 关闭：同一路径的焦点也要回按钮。
  await detailButton.click()
  await expect(drawer).toBeVisible()
  await drawer.getByRole('button', { name: '关闭详情' }).click()
  await expect(drawer).toBeHidden()
  await expect(detailButton).toBeFocused()

  // 长详情可键盘滚动：pre 可聚焦，PageDown 生效。
  await detailButton.click()
  const detailText = drawer.locator('.a-detail-text')
  await expect(detailText).toHaveAttribute('tabindex', '0')
  await detailText.focus()
  await expect(detailText).toBeFocused()
  const beforeScroll = await detailText.evaluate((element) => element.scrollTop)
  await page.keyboard.press('PageDown')
  await expect
    .poll(() => detailText.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(beforeScroll)
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  // 整行可点开详情，关闭后焦点回到行。
  const firstRow = page.locator('[data-audit-row]').first()
  await firstRow.locator('.a-time').click()
  await expect(drawer).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect(firstRow).toBeFocused()

  // 翻页后横向滚动归零，列表身份从第一列重新开始。
  const tableScroll = page.locator('.a-table-scroll')
  await tableScroll.evaluate((element) => {
    element.scrollLeft = element.scrollWidth
  })
  await page.getByRole('button', { name: '下一页' }).click()
  await expect(firstRow).toHaveAttribute('data-audit-id', String(AUDIT_LOGS[3].id))
  expect(await tableScroll.evaluate((element) => element.scrollLeft)).toBe(0)
})
