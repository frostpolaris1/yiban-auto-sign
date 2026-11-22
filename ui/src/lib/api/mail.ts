/**
 * **功能**
 * 管理面邮件（SMTP）设置与测试邮件接口客户端。
 *
 * **归属**
 * `lib/api` 契约适配层：无状态请求函数与 Query 键常量；类型全部来自 `schema.d.ts`。
 *
 * **口径**
 * 路径、字段与返回值与 `openapi.yaml` 对齐：
 * - `GET /api/admin/mail-settings` 归 master/admin，只回生效值与来源，密钥永不回显。
 * - `PUT /api/admin/mail-settings` 只归 master：字段缺省＝不动、显式 `null`＝删覆盖行回落环境变量、
 *   给值＝upsert；每次写都必须带操作者本人 `current_password`（缺或空 400、口令错 403、连续失败 429，
 *   被拒的复核零写入零审计）。
 * - `POST /api/admin/mail/test` 只归 master，收件人恒为登录主管理员本人；429 带 `Retry-After` 冷却。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type MailSettingSource = Schemas['MailSettingSource']
export type MailSettingValue = Schemas['MailSettingValue']
export type MailPortSetting = Schemas['MailPortSetting']
export type MailSensitiveSetting = Schemas['MailSensitiveSetting']
export type MailPasswordSetting = Schemas['MailPasswordSetting']
export type MailSettingsReport = Schemas['MailSettingsReport']
export type MailTLSMode = Schemas['MailTLSMode']
export type AdminMailSettingsUpdate = Schemas['AdminMailSettingsUpdate']
export type AdminMailTestRequest = Schemas['AdminMailTestRequest']
export type MailTestResult = Schemas['MailTestResult']

export const MAIL_SETTINGS_QUERY_KEY = ['admin-mail-settings'] as const

export const MAIL_PATHS = {
  settings: '/api/admin/mail-settings',
  test: '/api/admin/mail/test',
} as const

export function getMailSettings(): Promise<MailSettingsReport> {
  return apiRequest<MailSettingsReport>(MAIL_PATHS.settings)
}

/** 部分补丁：只提交要动的键。响应是写后的生效值。 */
export function updateMailSettings(body: AdminMailSettingsUpdate): Promise<MailSettingsReport> {
  return apiRequest<MailSettingsReport>(MAIL_PATHS.settings, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

/** 测试邮件先复核操作者口令；收件人不可指定。 */
export function sendTestMail(body: AdminMailTestRequest): Promise<MailTestResult> {
  return apiRequest<MailTestResult>(MAIL_PATHS.test, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
