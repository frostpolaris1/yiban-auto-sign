/**
 * `/system` 系统名册（#roster）与版本自检（#version）行为守卫（04d 只读收尾批）。
 *
 * 断言用户可见不变式：
 * 1. 名册恰一次 `GET /api/admin/settings` 全分组渲染；来源三态如实；
 *    数据态零编辑控件（input/select/textarea/button/switch 计数为 0）；读取失败给
 *    类别化文案与重试，重试后恢复且底层错误原文不回显；
 *    保存调度策略后 PUT 的全量名册写入共享缓存：调度值更新、其它组不丢、不补发名册请求。
 * 2. 敏感键只显「已设置 / 未设置＋固定掩码＋来源」；负例：畸形出包带原值也不渲染
 *    （innerText 与整页 HTML 均不含该串）。
 * 3. 版本区：前端版本取构建期常量（与 `ui/package.json` 同值）；后端版本无真来源，
 *    只陈述事实不造数；自检恰一次 `GET /healthz`，失败显式可重试。
 * 4. `admin` 可读名册全分组且零写请求。
 *    两页 user 页级拒绝与 390 巡检分别已由 `settings.spec.ts` 与
 *    `system-policy.spec.ts` 的同族用例守卫，此处不重复设门。
 */

import { readFileSync } from 'node:fs'
import { expect, test, type Page } from '@playwright/test'
import type { SettingsReport } from '../src/lib/api/adminSettings'
import { SCHEDULE_DAY, mockScheduleApi } from './support/fake-schedule-api'

const pkgVersion: string = JSON.parse(
  readFileSync(new URL('../package.json', import.meta.url), 'utf8'),
).version

async function visitSystem(
  page: Page,
  options: Parameters<typeof mockScheduleApi>[1] = { role: 'master' },
) {
  const api = await mockScheduleApi(page, options)
  await page.goto(`/system?day=${SCHEDULE_DAY}`)
  await expect(page.getByRole('heading', { level: 1 })).toContainText('系统设置')
  return api
}

function callsTo(calls: { method: string; path: string }[], method: string, path: string) {
  return calls.filter((call) => call.method === method && call.path === path)
}

test('roster renders all groups with exactly one request and zero edit controls', async ({
  page,
}) => {
  const api = await visitSystem(page, { role: 'master' })
  const roster = page.locator('#roster')

  await expect(roster).toContainText('YIBAN_MAIL_PASS')
  for (const group of ['调度', '通知', '部署']) {
    await expect(roster.getByRole('region', { name: `名册分组 ${group}` })).toBeVisible()
  }
  // 三态来源如实：env / db / default / unknown 各至少一枚徽章。
  for (const label of ['环境变量', '数据库覆盖', '默认', '未知']) {
    await expect(roster.getByText(label).first()).toBeVisible()
  }
  // 非敏感值如实：字符串、数字与空串各显其形。
  await expect(roster.getByText(':8080').first()).toBeVisible()
  await expect(roster.getByText('60').first()).toBeVisible()
  await expect(roster.getByText('空字符串').first()).toBeVisible()

  expect(callsTo(api.calls, 'GET', '/api/admin/settings')).toHaveLength(1)

  // 只读分区：数据态不得出现任何编辑控件或按钮。
  for (const selector of ['input', 'select', 'textarea', 'button', '[role="switch"]']) {
    await expect(roster.locator(selector)).toHaveCount(0)
  }
})

test('sensitive keys never leak original values even in a malformed payload', async ({ page }) => {
  const secret = 'synthetic-plain-secret-4f9a'
  const malformed: SettingsReport = {
    groups: [
      {
        name: '通知',
        items: [
          {
            key: 'YIBAN_MAIL_PASS',
            group: '通知',
            source: 'env',
            // 负例：敏感键被畸形出包带上原值。视图必须只消费 sensitive。
            value: secret,
            sensitive: { set: true, masked: '****', source: 'env' },
          },
        ],
      },
    ],
  }
  await visitSystem(page, { role: 'master', settings: malformed })
  const roster = page.locator('#roster')
  await expect(roster.getByText('****').first()).toBeVisible()
  await expect(roster.getByText('已设置').first()).toBeVisible()

  const bodyText = await page.locator('body').innerText()
  expect(bodyText).not.toContain(secret)
  const html = await page.content()
  expect(html).not.toContain(secret)
})

test('roster read failure shows a classed alert with retry and recovers without echoing the wire error', async ({
  page,
}) => {
  const queue = [
    { status: 500, code: 'internal_error', message: 'roster exploded' },
    { status: 500, code: 'internal_error', message: 'roster exploded' },
    undefined,
  ]
  const api = await visitSystem(page, {
    role: 'master',
    onSettingsRead: () => queue.shift(),
  })
  const roster = page.locator('#roster')

  // 初次失败 + 1 次自动重试后落错误态：只报类别，不回显服务端原文。
  await expect(roster.getByRole('alert')).toContainText('名册读取失败，请稍后重试')
  await expect(roster.getByRole('alert')).not.toContainText('roster exploded')
  const retry = roster.getByRole('button', { name: '重试' })
  await expect(retry).toBeVisible()
  await retry.click()

  await expect(roster).toContainText('YIBAN_MAIL_PASS')
  // 1 次初始 + 1 次自动重试 + 1 次手动重试。
  expect(callsTo(api.calls, 'GET', '/api/admin/settings')).toHaveLength(3)
})

test('schedule save refreshes the shared roster cache with the full report, keeping other groups', async ({
  page,
}) => {
  const api = await visitSystem(page, { role: 'master' })
  const roster = page.locator('#roster')
  const scheduleGroup = roster.getByRole('region', { name: '名册分组 调度' })
  const frontRow = scheduleGroup.locator('li').filter({ hasText: 'YIBAN_WINDOW_EDGE_FRONT_SEC' })

  await expect(frontRow).toContainText('60')
  await expect(roster.getByRole('region', { name: '名册分组 通知' })).toContainText(
    'YIBAN_MAIL_PASS',
  )
  expect(callsTo(api.calls, 'GET', '/api/admin/settings')).toHaveLength(1)

  // 保存调度策略：PUT 回传全量名册并写入共享 ['admin-settings'] 缓存。
  const card = page.locator('#policy-schedule-card')
  await card.getByLabel('前边界缩进（秒）').fill('45')
  await card.getByRole('button', { name: '保存调度策略' }).click()
  await expect(page.getByText('调度运行策略已保存')).toBeVisible()

  // 调度组取新值，通知/部署组不因写回丢组；名册不补发 GET（缓存共享路径）。
  await expect(frontRow).toContainText('45')
  await expect(frontRow).toContainText('数据库覆盖')
  await expect(roster.getByRole('region', { name: '名册分组 通知' })).toContainText(
    'YIBAN_MAIL_PASS',
  )
  await expect(roster.getByRole('region', { name: '名册分组 部署' })).toContainText(
    'YIBAN_HTTP_ADDR',
  )
  expect(callsTo(api.calls, 'GET', '/api/admin/settings')).toHaveLength(1)
})

test('version section shows build-time UI version, states backend absence honestly, and self-checks via /healthz', async ({
  page,
}) => {
  const api = await visitSystem(page, { role: 'master' })
  const version = page.locator('#version')

  await expect(version.getByText('前端版本').first()).toBeVisible()
  await expect(version).toContainText(`v${pkgVersion}`)
  // 后端版本无 HTTP 来源：限定后端版本行，值格精确为「暂无来源」，
  // 且整行不出现版本形状数字（防回归悄悄渲染臆造版本号）。
  const backendRow = version.locator('.sys-version-row').filter({ hasText: '后端版本' })
  await expect(backendRow.locator('.sys-version-value')).toHaveText('暂无来源')
  await expect(backendRow).toContainText('不渲染数值')
  await expect(backendRow).not.toContainText(/\d+\.\d+/)

  // 自检成功态恰一次探针请求。
  await expect(version).toContainText('进程在应答')
  expect(callsTo(api.calls, 'GET', '/healthz')).toHaveLength(1)
})

test('health self-check failure is explicit and retryable', async ({ page }) => {
  const queue = [
    { status: 503, code: 'unavailable', message: 'probe exploded' },
    { status: 503, code: 'unavailable', message: 'probe exploded' },
    undefined,
  ]
  await visitSystem(page, {
    role: 'master',
    onHealth: () => queue.shift(),
  })
  const version = page.locator('#version')

  await expect(version.getByRole('alert')).toContainText('自检失败：服务未在应答')
  await version.getByRole('button', { name: '重试' }).click()
  await expect(version).toContainText('进程在应答')
})

test('admin reads the full roster masked, with zero write requests', async ({ page }) => {
  const api = await visitSystem(page, { role: 'admin' })
  const roster = page.locator('#roster')

  await expect(roster).toContainText('YIBAN_WEBHOOK_SECRET')
  await expect(roster.getByText('****').first()).toBeVisible()
  await expect(roster.getByText('未设置').first()).toBeVisible()
  const writes = api.calls.filter((call) => call.method === 'PUT' || call.method === 'POST')
  expect(writes).toEqual([])
})
