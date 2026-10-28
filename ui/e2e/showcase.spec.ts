import { test, expect } from '@playwright/test'

/**
 * 展示模式的唯一不变式。
 *
 * `VITE_SHOWCASE=1` 构建下，看板走合成会话（master）与合成夹具：
 * 卡片有数据，且零 `/api/` 请求（会话探针也不发）。
 * 该构建由 4174 端口的预览服务提供；构建命令见 `package.json` 的 `build:showcase`。
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
  await expect(total.locator('.kpi-value')).toHaveText(/^4\s*项$/)

  const card = page.locator('.card').filter({ hasText: '容量体检' })
  await expect(page.getByLabel('任务')).toHaveValue('school-a:task-daily')
  await expect(card.locator('.app-metric').filter({ hasText: '容量' })).toContainText('24')
  await expect(card.locator('.app-metric').filter({ hasText: '需求' })).toContainText('8')

  expect(apiRequests, 'the showcase build never touches the API').toEqual([])
})
