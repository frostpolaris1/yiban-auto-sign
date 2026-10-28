import { test, expect } from '@playwright/test'
import { visibleText } from './support/visible-text'

/**
 * 展示模式的两条不变式。
 *
 * 1. `VITE_SHOWCASE=1` 构建下，看板走合成会话（master）与合成夹具：
 *    卡片有数据，且零 `/api/` 请求（会话探针也不发）。
 *    该构建由 4174 端口的预览服务提供；构建命令见 `package.json` 的 `build:showcase`。
 * 2. 微交互在展示构建里照常装配：toast 容器在 DOM 里，数字有夹具终值，
 *    列表过渡不抛错。展示机没有写操作桩，成功 toast 的端到端触发不在此测
 *    （写请求会打到不存在的后端），这条已在汇报里登记为未测维度。
 */

test.use({ baseURL: 'http://127.0.0.1:4174' })

test('invariant 7: the showcase dashboard renders fixtures with zero requests', async ({
  page,
}) => {
  const apiRequests: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname.startsWith('/api/')) apiRequests.push(url.pathname)
  })

  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('签到运行')

  const total = page.locator('.kpi-card').filter({ hasText: '任务总数' })
  await expect.poll(() => visibleText(total.locator('.kpi-value'))).toBe('4项')

  const card = page.locator('.card').filter({ hasText: '容量体检' })
  await expect(page.getByLabel('任务')).toHaveValue('school-a:task-daily')
  await expect
    .poll(() => visibleText(card.locator('.app-metric').filter({ hasText: '容量' })))
    .toBe('容量24')
  await expect
    .poll(() => visibleText(card.locator('.app-metric').filter({ hasText: '需求' })))
    .toBe('需求8')

  expect(apiRequests, 'the showcase build never touches the API').toEqual([])
})

test('invariant 6: the showcase build mounts the toast host and reports no page error', async ({
  page,
}) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))

  await page.goto('/accounts')
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()
  // 无 toast 时 sonner 只渲染常驻的实时区域；列表容器随首条 toast 挂载。
  await expect(page.locator('section[aria-live="polite"]')).toHaveCount(1)
  expect(errors, 'the animated table and the toast host raise no page error').toEqual([])
})
