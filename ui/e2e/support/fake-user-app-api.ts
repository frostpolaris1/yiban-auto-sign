import type { Page } from '@playwright/test'
import type { Account, AccountDetectResult } from '../../src/lib/api/account'
import type {
  DiagnosticsItem,
  SignCalendar,
  SignCalendarDay,
  SignStatusItem,
} from '../../src/lib/api/me'
import type { TimePref } from '../../src/lib/api/schedule'
import { mockAccountsApi, type FakeApi, type MockOptions } from './fake-accounts-api'

/**
 * 用户端首页的假 API。
 *
 * 先装账号页的 catch-all，再覆盖本人读面：
 * `me/sign-status`、`me/diagnostics`、`me/time-pref`、`me/sign-calendar`、`announcement`。
 * 只返回登记过的合成值：账号 `acc-user`、手机号掩码、固定时间。
 * 路由注册顺序即 LIFO：本文件的专用路由必须后装。
 */

export const USER_ACCOUNT: Account = {
  id: 'acc-user',
  school_id: 'school-a',
  owner_user_id: 'user-1',
  owner_email_masked: 'o***@example.com',
  name: '东区宿舍',
  display_name: '东区宿舍',
  phone_masked: '138****8000',
  phone_model: 'Pixel 7',
  status: 'active',
  deleted_at: null,
  reject_reason: '',
  reviewed_at: null,
  reviewed_by: null,
  created_at: '2026-10-01T01:00:00Z',
  updated_at: '2026-11-11T07:02:00Z',
}

export const USER_SIGN_STATUS: SignStatusItem[] = [
  {
    account_id: USER_ACCOUNT.id,
    display_name: USER_ACCOUNT.display_name,
    phone_masked: USER_ACCOUNT.phone_masked,
    account_status: 'active',
    state: 'success',
    run_at: '2026-11-11T07:02:00+08:00',
    stale: false,
  },
]

export const USER_DIAGNOSTICS: DiagnosticsItem[] = [
  {
    account_id: USER_ACCOUNT.id,
    display_name: USER_ACCOUNT.display_name,
    phone_masked: USER_ACCOUNT.phone_masked,
    account_status: 'active',
    state: 'success',
    run_at: '2026-11-11T07:02:00+08:00',
    attempts: 1,
    result: 'ok',
    next_retry_at: null,
    suggestion: 'wait',
  },
]

function slotLabel(index: number): string {
  const minutes = 6 * 60 + 30 + index * 5
  const hour = String(Math.floor(minutes / 60)).padStart(2, '0')
  const minute = String(minutes % 60).padStart(2, '0')
  return `${hour}:${minute}`
}

export const USER_TIME_PREF: TimePref = {
  offset_min: 15,
  allowed: true,
  window: { start: '06:30', end: '07:50' },
  slots: Array.from({ length: 16 }, (_, index) => ({
    offset_min: index * 5,
    label: slotLabel(index),
    disabled: false,
    pct: index === 3 ? 40 : 0,
    edge_note: '',
  })),
  estimated: '06:45',
  estimate_note: '（今日计划，非承诺）',
}

function addDays(day: string, amount: number): string {
  const date = new Date(`${day}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + amount)
  return date.toISOString().slice(0, 10)
}

/** 7 天全成功；今天标记 future=false，其余天数同样有记录。 */
export function userCalendar(from: string, to: string): SignCalendar {
  const days: SignCalendarDay[] = []
  let cursor = from
  while (cursor <= to) {
    days.push({
      day: cursor,
      total: 1,
      success: 1,
      failed: 0,
      skipped: 0,
      unknown: 0,
      future: false,
    })
    cursor = addDays(cursor, 1)
  }
  return {
    timezone: 'Asia/Shanghai',
    today: to,
    updated_at: '2026-11-11T07:02:00+08:00',
    days,
  }
}

export type UserAppApi = FakeApi

export type UserAppOptions = MockOptions & {
  signStatus?: SignStatusItem[]
  diagnostics?: DiagnosticsItem[]
  timePref?: TimePref
  announcement?: string
  registrationEnabled?: boolean
  detect?: AccountDetectResult
}

export async function mockUserAppApi(
  page: Page,
  options: UserAppOptions = { role: 'user' },
): Promise<UserAppApi> {
  const api = await mockAccountsApi(page, {
    ...options,
    // null 是「未登录」，不能被 ?? 吃掉。
    role: options.role === undefined ? 'user' : options.role,
    accounts: options.accounts ?? [USER_ACCOUNT],
    deletedAccounts: options.accounts ? [] : options.deletedAccounts,
  })

  const status = options.signStatus ?? USER_SIGN_STATUS
  const diagnostics = options.diagnostics ?? USER_DIAGNOSTICS
  let timePref = options.timePref ?? USER_TIME_PREF
  const announcement = options.announcement ?? ''
  let mailEnabled = true
  let retentionDays: 30 | 90 | 180 | 365 = 90

  await page.route('**/api/me/sign-status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ updated_at: '2026-11-11T07:02:00+08:00', items: status }),
    }),
  )

  await page.route('**/api/me/diagnostics', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ updated_at: '2026-11-11T07:02:00+08:00', items: diagnostics }),
    }),
  )

  await page.route('**/api/me/time-pref', (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as { offset_min: number | null }
      timePref = { ...timePref, offset_min: body.offset_min }
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(timePref),
    })
  })

  await page.route('**/api/me/mail-notify', (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as { enabled: boolean }
      mailEnabled = body.enabled
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ enabled: mailEnabled }),
    })
  })

  await page.route('**/api/me/photo-retention', (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as { days: 30 | 90 | 180 | 365 }
      retentionDays = body.days
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ days: retentionDays }),
    })
  })

  await page.route('**/api/auth/password-policy', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ min_chars: 10, min_classes: 2, max_bytes: 1024 }),
    }),
  )

  await page.route('**/api/me/sign-calendar**', (route) => {
    const url = new URL(route.request().url())
    const from = url.searchParams.get('from') ?? '2026-11-05'
    const to = url.searchParams.get('to') ?? '2026-11-11'
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(userCalendar(from, to)),
    })
  })

  await page.route('**/api/accounts/detect', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        options.detect ?? {
          university_name: '合成大学',
          university_id: 'syn-university',
          school: { id: 'school-a', name: '合成学校甲' },
          person_name: '合成同学',
          device_model: 'Pixel 7',
          device_code_present: true,
        },
      ),
    }),
  )

  await page.route('**/api/accounts/*/location', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ mode: 'auto', lng: null, lat: null, reason: '' }),
    }),
  )

  await page.route('**/api/me/restore', (route) =>
    route.fulfill({ status: 204, body: '' }),
  )

  await page.route('**/api/me/delete', (route) =>
    route.fulfill({ status: 204, body: '' }),
  )

  await page.route('**/api/me/accounts/*/delete', (route) =>
    route.fulfill({ status: 204, body: '' }),
  )

  await page.route('**/api/me/accounts/*/restore', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(USER_ACCOUNT),
    }),
  )

  await page.route('**/api/auth/registration', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        enabled: options.registrationEnabled ?? true,
        terms_version: 1,
        privacy_version: 1,
      }),
    }),
  )

  await page.route('**/api/me/sign-calendar/*', (route) => {
    const url = new URL(route.request().url())
    const day = url.pathname.split('/').pop() ?? ''
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        day,
        timezone: 'Asia/Shanghai',
        future: false,
        accounts: [
          {
            account_id: USER_ACCOUNT.id,
            state: 'success',
            run_at: `${day}T07:02:00+08:00`,
            attempts: 1,
          },
        ],
      }),
    })
  })

  await page.route('**/api/announcement', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        text: announcement,
        updated_at: announcement === '' ? null : '2026-11-11T06:00:00+08:00',
      }),
    }),
  )

  return api
}
