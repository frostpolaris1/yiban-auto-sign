/**
 * **功能**
 * 系统运行策略、配额、名册与投递渠道自检接口客户端。
 *
 * **归属**
 * `lib/api` 契约适配层：无状态请求函数与 Query 键常量；类型全部来自 `schema.d.ts`。
 *
 * **口径**
 * 所有路径、字段与返回值与 `openapi.yaml` 对齐：
 * - 调度设置读回全部 8 键及其来源（`db` / `env` / `default`），写回返回配置名册 `SettingsReport`。
 * - 配额写回为部分补丁，传 `null` 删覆盖行恢复默认 1；传 0 表示不限。
 * - 测试邮件在 body 中显式传 `current_password`，不依赖隐式会话；测试 Webhook 无请求体。
 */

import { apiRequest } from './client'
import type { components } from './schema.d'

export type RegistrationStatus = components['schemas']['RegistrationStatus']
export type RegistrationSetRequest = components['schemas']['RegistrationSetRequest']
export type AutoApproveStatus = components['schemas']['AutoApproveStatus']
export type AutoApproveUpdate = components['schemas']['AutoApproveUpdate']
export type QuotaReport = components['schemas']['QuotaReport']
export type QuotaSetting = components['schemas']['QuotaSetting']
export type QuotaSettingSource = QuotaSetting['source']
export type AdminQuotaSettingsUpdate = components['schemas']['AdminQuotaSettingsUpdate']
export type ScheduleSettingsReport = components['schemas']['ScheduleSettingsReport']
export type AdminScheduleSettingsUpdate = components['schemas']['AdminScheduleSettingsUpdate']
export type ScheduleSettingSource = components['schemas']['ScheduleSettingSource']
export type ScheduleSignDist = components['schemas']['ScheduleSignDist']
export type SettingsReport = components['schemas']['SettingsReport']
export type SettingGroup = components['schemas']['SettingGroup']
export type SettingItem = components['schemas']['SettingItem']
export type SettingSource = components['schemas']['SettingSource']
export type AdminMailTestRequest = components['schemas']['AdminMailTestRequest']
export type MailTestResult = components['schemas']['MailTestResult']
export type WebhookTestResult = components['schemas']['WebhookTestResult']

export const REGISTRATION_STATUS_QUERY_KEY = ['auth-registration'] as const
export const AUTO_APPROVE_QUERY_KEY = ['admin-auto-approve'] as const
export const QUOTA_SETTINGS_QUERY_KEY = ['admin-quota-settings'] as const
export const SCHEDULE_SETTINGS_QUERY_KEY = ['admin-schedule-settings'] as const
export const ADMIN_SETTINGS_QUERY_KEY = ['admin-settings'] as const

export function getRegistrationStatus(): Promise<RegistrationStatus> {
  return apiRequest<RegistrationStatus>('/api/auth/registration')
}

export function updateRegistrationStatus(
  body: RegistrationSetRequest,
): Promise<RegistrationStatus> {
  return apiRequest<RegistrationStatus>('/api/auth/registration', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function getAutoApprove(): Promise<AutoApproveStatus> {
  return apiRequest<AutoApproveStatus>('/api/admin/auto-approve')
}

export function updateAutoApprove(body: AutoApproveUpdate): Promise<AutoApproveStatus> {
  return apiRequest<AutoApproveStatus>('/api/admin/auto-approve', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function getQuotaSettings(): Promise<QuotaReport> {
  return apiRequest<QuotaReport>('/api/admin/quota-settings')
}

export function updateQuotaSettings(body: AdminQuotaSettingsUpdate): Promise<QuotaReport> {
  return apiRequest<QuotaReport>('/api/admin/quota-settings', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function getScheduleSettings(): Promise<ScheduleSettingsReport> {
  return apiRequest<ScheduleSettingsReport>('/api/admin/schedule-settings')
}

export function updateScheduleSettings(
  body: AdminScheduleSettingsUpdate,
): Promise<SettingsReport> {
  return apiRequest<SettingsReport>('/api/admin/schedule-settings', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function getAdminSettings(): Promise<SettingsReport> {
  return apiRequest<SettingsReport>('/api/admin/settings')
}

export function sendTestMail(body: AdminMailTestRequest): Promise<MailTestResult> {
  return apiRequest<MailTestResult>('/api/admin/mail/test', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function sendTestWebhook(): Promise<WebhookTestResult> {
  return apiRequest<WebhookTestResult>('/api/admin/webhook/test', {
    method: 'POST',
  })
}
