/**
 * **功能**
 * 本人签到状态与诊断 API：类型别名、路径表与两个只读查询。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写契约形状。
 * `sign-status` 的 `x-roles` 只有 user；master/admin 调用收到 403。
 * `diagnostics` 的 `x-roles` 是 user 与 admin；看板只对 user 挂载。
 * `state` 是契约文档词汇，不是枚举：未知值原样传给展示层。
 * `suggestion` 是契约枚举，类型层面穷尽四值。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type SignStatusReport = Schemas['SignStatusReport']
export type SignStatusItem = Schemas['SignStatusItem']
export type DiagnosticsReport = Schemas['DiagnosticsReport']
export type DiagnosticsItem = Schemas['DiagnosticsItem']
export type DiagnosticsSuggestion = DiagnosticsItem['suggestion']
export type SignCalendar = Schemas['SignCalendar']
export type SignCalendarDay = Schemas['SignCalendarDay']
export type SignCalendarDayDetail = Schemas['SignCalendarDayDetail']
export type SignCalendarAccount = Schemas['SignCalendarAccount']
export type MailNotify = Schemas['MailNotify']
export type PhotoRetention = Schemas['PhotoRetention']

export const ME_PATHS = {
  signStatus: '/api/me/sign-status',
  diagnostics: '/api/me/diagnostics',
  signCalendar: '/api/me/sign-calendar',
  signCalendarDay: (day: string) => `/api/me/sign-calendar/${encodeURIComponent(day)}`,
  mailNotify: '/api/me/mail-notify',
  photoRetention: '/api/me/photo-retention',
} as const

/** 轻量状态，供 30 秒轮询。 */
export function getSignStatus(): Promise<SignStatusReport> {
  return apiRequest<SignStatusReport>(ME_PATHS.signStatus)
}

/** 诊断建议。建议值是契约枚举，展示层按四值映射中文标签。 */
export function getDiagnostics(): Promise<DiagnosticsReport> {
  return apiRequest<DiagnosticsReport>(ME_PATHS.diagnostics)
}

/** 30 天日历；契约要求 from/to 必填且跨度不超过 31 天。 */
export function getSignCalendar(from: string, to: string): Promise<SignCalendar> {
  const params = new URLSearchParams({ from, to })
  return apiRequest<SignCalendar>(`${ME_PATHS.signCalendar}?${params.toString()}`)
}

/** 某日明细；未来日返回 future=true 且 accounts 为空。 */
export function getSignCalendarDay(day: string): Promise<SignCalendarDayDetail> {
  return apiRequest<SignCalendarDayDetail>(ME_PATHS.signCalendarDay(day))
}

/** 签到失败邮件开关；默认 true，只发本人失败。 */
export function getMailNotify(): Promise<MailNotify> {
  return apiRequest<MailNotify>(ME_PATHS.mailNotify)
}

export function setMailNotify(enabled: boolean): Promise<MailNotify> {
  return apiRequest<MailNotify>(ME_PATHS.mailNotify, {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  })
}

/** 照片保留期；30/90/180/365 天。 */
export function getPhotoRetention(): Promise<PhotoRetention> {
  return apiRequest<PhotoRetention>(ME_PATHS.photoRetention)
}

export function setPhotoRetention(days: PhotoRetention['days']): Promise<PhotoRetention> {
  return apiRequest<PhotoRetention>(ME_PATHS.photoRetention, {
    method: 'PUT',
    body: JSON.stringify({ days }),
  })
}
