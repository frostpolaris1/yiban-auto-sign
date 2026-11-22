/**
 * **功能**
 * 邮件（SMTP）设置卡的纯逻辑：草稿构造、字段校验、部分补丁拼装与错误中文话术。
 *
 * **归属**
 * `features/settings` 纯逻辑层；卡片组件只负责呈现与调用。
 *
 * **口径**
 * - 表单只编辑契约允许的六个键；`host` / `port` / `tls_mode` 是非敏感项，读回来的生效值可直接预填，
 *   只有与生效值不同时才进补丁（缺省＝不动，避免无意义的覆盖行）。
 * - `from` / `user` / `pass` 是 write-only：服务端永不回显原值，输入框始终为空，
 *   留空＝不修改，填写才进补丁。密码不回填、不缓存、不写日志。
 * - 每次写都必须带操作者本人 `current_password`（缺或空 400、口令错 403、连续失败 429）。
 *   本层只校验非空，口令正确与否以服务端返回为准。
 * - 后端 `code · message` 原文不外泄，一律按 HTTP 状态收口成中文。
 */

import { ApiError } from '@/lib/api/client'
import type {
  AdminMailSettingsUpdate,
  MailSettingsReport,
  MailSensitiveSetting,
  MailSettingSource,
  MailTLSMode,
} from '@/lib/api/mail'

export const MAIL_TLS_OPTIONS: ReadonlyArray<{ value: MailTLSMode; label: string }> = [
  { value: 'auto', label: '自动（465 走隐式 TLS，其余 STARTTLS）' },
  { value: 'implicit', label: '隐式 TLS（连接即加密）' },
  { value: 'starttls', label: 'STARTTLS（明文起步再升级）' },
]

export const MAIL_SAVED_TOAST = '邮件设置已保存'
export const MAIL_TEST_TOAST = '测试邮件已提交'
export const MAIL_NETWORK_ERROR = '网络异常，请稍后重试'

const SOURCE_LABELS: Record<MailSettingSource, string> = {
  db: '数据库覆盖',
  env: '环境变量',
  default: '默认值',
}

/** 生效值的来源：告诉操作者这条值是从哪一层来的。 */
export function mailSourceLabel(source: MailSettingSource): string {
  return SOURCE_LABELS[source] ?? source
}

/** 敏感项只报「是否已设置」，原值永不显示。 */
export function mailSensitiveText(setting: MailSensitiveSetting): string {
  return setting.set ? '已设置（不显示原值）' : '未设置'
}

/** 密码项连掩码都没有，只有 set 标志。 */
export function mailPasswordText(setting: MailSettingsReport['pass']): string {
  return setting.set ? '已设置（不显示原值）' : '未设置'
}

export function isMailTLSMode(value: string): value is MailTLSMode {
  return value === 'auto' || value === 'implicit' || value === 'starttls'
}

export type MailSettingsDraft = {
  host: string
  port: string
  tlsMode: string
  from: string
  user: string
  pass: string
  currentPassword: string
}

/** 草稿初值：非敏感项预填生效值，敏感项与口令一律留空。 */
export function mailSettingsDraft(report: MailSettingsReport | undefined): MailSettingsDraft {
  return {
    host: report?.host.value ?? '',
    port: report ? String(report.port.value) : '',
    tlsMode: report?.tls_mode.value ?? 'auto',
    from: '',
    user: '',
    pass: '',
    currentPassword: '',
  }
}

export type MailDraftField = 'host' | 'port' | 'from' | 'user' | 'pass' | 'currentPassword'

const MAIL_ADDRESS_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

/** 字段级校验：返回空对象表示可以提交。 */
export function mailDraftFieldErrors(draft: MailSettingsDraft): Partial<Record<MailDraftField, string>> {
  const errors: Partial<Record<MailDraftField, string>> = {}
  const host = draft.host.trim()
  if (host === '') errors.host = '请输入 SMTP 主机名'
  const portText = draft.port.trim()
  if (portText === '') {
    errors.port = '请输入端口'
  } else if (!/^\d+$/.test(portText)) {
    errors.port = '端口必须是整数'
  } else {
    const port = Number(portText)
    if (port < 1 || port > 65535) errors.port = '端口必须在 1 到 65535 之间'
  }
  const from = draft.from.trim()
  if (from !== '' && !MAIL_ADDRESS_RE.test(from)) errors.from = '请填写可投递的发件地址'
  const user = draft.user.trim()
  if (draft.user !== '' && user === '') errors.user = '账号不能只有空白字符'
  if (draft.pass !== '' && draft.pass.trim() === '') errors.pass = '密码不能只有空白字符'
  if (draft.currentPassword.trim() === '') errors.currentPassword = '请输入你的登录口令'
  return errors
}

/**
 * 拼部分补丁：只放真正要动的键。
 * 三项非敏感值与原生效值相同就不带；三项敏感值留空就不带。
 * 返回 `null` 表示没有任何设置变更（只剩口令），调用方不该发请求。
 */
export function buildMailSettingsUpdate(
  report: MailSettingsReport,
  draft: MailSettingsDraft,
  currentPassword: string,
): AdminMailSettingsUpdate | null {
  const body: AdminMailSettingsUpdate = { current_password: currentPassword }
  let changed = false

  const host = draft.host.trim()
  if (host !== '' && host !== report.host.value) {
    body.host = host
    changed = true
  }

  const portText = draft.port.trim()
  if (portText !== '') {
    const port = Number(portText)
    if (port !== report.port.value) {
      body.port = port
      changed = true
    }
  }

  const tlsMode = draft.tlsMode
  if (isMailTLSMode(tlsMode) && tlsMode !== report.tls_mode.value) {
    body.tls_mode = tlsMode
    changed = true
  }

  const from = draft.from.trim()
  if (from !== '') {
    body.from = from
    changed = true
  }
  const user = draft.user.trim()
  if (user !== '') {
    body.user = user
    changed = true
  }
  if (draft.pass !== '') {
    body.pass = draft.pass
    changed = true
  }

  return changed ? body : null
}

function retryAfterSeconds(error: ApiError): number | undefined {
  if (typeof error.retryAfterSec === 'number' && error.retryAfterSec > 0) return error.retryAfterSec
  if (error.details && typeof error.details === 'object') {
    const raw = (error.details as { retry_after_seconds?: unknown }).retry_after_seconds
    if (typeof raw === 'number' && Number.isFinite(raw) && raw > 0) return Math.ceil(raw)
  }
  return undefined
}

/** 保存失败的卡片内文案：400 缺口令、403 口令错或非主管理员、429 复核节流。 */
export function mailSettingsErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return MAIL_NETWORK_ERROR
  switch (error.status) {
    case 400:
      return '保存被拒绝：口令不能为空，或字段不合法'
    case 401:
      return '登录已过期，请重新登录后再试'
    case 403:
      return '保存被拒绝：登录口令不正确，或当前账号不是主管理员'
    case 429: {
      const wait = retryAfterSeconds(error)
      return wait ? `复核尝试过多，请在 ${wait} 秒后重试` : '复核尝试过多，请稍后重试'
    }
    case 409:
      return '保存与当前状态冲突，请刷新后重试'
    default:
      return error.status >= 500 ? '服务暂不可用，请稍后重试' : '保存失败，请稍后重试'
  }
}

/** 测试邮件失败的卡片内文案：409 未开启或无邮箱、429 冷却、502 投递失败。 */
export function mailTestErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return MAIL_NETWORK_ERROR
  switch (error.status) {
    case 400:
      return '发送被拒绝：请先填写你的登录口令'
    case 401:
      return '登录已过期，请重新登录后再试'
    case 403:
      return '发送被拒绝：登录口令不正确，或当前账号不是主管理员'
    case 409:
      return '无法发送测试邮件：SMTP 未开启，或当前主管理员账号未绑定邮箱'
    case 429: {
      const wait = retryAfterSeconds(error)
      return wait ? `测试邮件冷却中，请在 ${wait} 秒后重试` : '测试邮件冷却中，请稍后重试'
    }
    case 502:
      return '测试邮件投递失败，请检查 SMTP 配置与网络连通'
    default:
      return error.status >= 500 ? '服务暂不可用，请稍后重试' : '测试邮件发送失败，请稍后重试'
  }
}
