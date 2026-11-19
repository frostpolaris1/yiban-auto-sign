/**
 * **功能**
 * 通知渠道测试（#channels）的纯错误分类与等待时长格式化逻辑。
 *
 * **归属**
 * `features/system/channels` 纯逻辑层。
 *
 * **口径**
 * - `POST /api/admin/mail/test` 若在二次复核阶段失败（400 缺口令、403 口令错误、
 *   429 reauth 节流），错误必须留在二次复核模态框内。
 * - 若通过复核后在冷却、通道未配置或投递阶段失败（409 未配置/无邮箱、429 冷却、502 投递失败），
 *   必须关闭模态框并把反馈直接展示在「测试邮件」按钮旁。
 */

import { ApiError } from '@/lib/api/client'

export function extractRetryAfterSeconds(error: ApiError): number | undefined {
  if (typeof error.retryAfterSec === 'number' && error.retryAfterSec > 0) {
    return error.retryAfterSec
  }
  if (error.details && typeof error.details === 'object') {
    const raw = (error.details as { retry_after_seconds?: unknown }).retry_after_seconds
    if (typeof raw === 'number' && Number.isFinite(raw) && raw > 0) {
      return Math.ceil(raw)
    }
  }
  return undefined
}

function isReauthThrottleError(error: ApiError): boolean {
  const hasCooldownDetails =
    Boolean(error.details && typeof error.details === 'object' && 'retry_after_seconds' in error.details)
  if (hasCooldownDetails) return false
  const msg = error.message.toLowerCase()
  return msg.includes('reauth') || msg.includes('attempt')
}

/** 判定测试邮件错误是否属于「二次密码复核错误」，从而保留弹窗而不外落。 */
export function shouldKeepMailTestModalOpen(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true
  if (error.status === 400) return true
  if (error.status === 403) {
    // 排除权限拒绝与 CSRF 校验失败；仅将口令复核失败留在弹窗内。
    const msg = error.message.toLowerCase()
    return !msg.includes('master role') && !msg.includes('csrf')
  }
  if (error.status === 429) {
    return isReauthThrottleError(error)
  }
  return false
}

/**
 * 二次复核弹窗内的失败文案。已知状态给对应中文话术，其余兜底中文；
 * 后端 `code · message` 原文不外泄。
 */
export function formatMailReauthModalError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) {
      return '请输入当前登录密码'
    }
    if (error.status === 401) {
      return '登录已过期，请重新登录后再试'
    }
    if (error.status === 403) {
      return '当前登录密码不正确，请核对后重试'
    }
    if (error.status === 429) {
      const wait = extractRetryAfterSeconds(error)
      return wait
        ? `密码复核尝试过多，请在 ${wait} 秒后重试`
        : '密码复核尝试过多，请稍后重试'
    }
    return '验证失败，请稍后重试'
  }
  return '网络异常或服务不可达，请稍后重试'
}

/** 测试邮件外落按钮旁的失败文案；已知状态中文话术，其余兜底中文。 */
export function formatMailTestResultError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return '登录已过期，请重新登录后再试'
    }
    if (error.status === 409) {
      return '无法发送测试邮件：SMTP 未开启或当前主管理员账号未绑定邮箱'
    }
    if (error.status === 429) {
      const wait = extractRetryAfterSeconds(error)
      return wait ? `测试邮件冷却中，请在 ${wait} 秒后重试` : '测试邮件冷却中，请稍后重试'
    }
    if (error.status === 502) {
      return '测试邮件投递失败，请检查 SMTP 配置与网络连通'
    }
    if (error.status === 403) {
      if (error.message.toLowerCase().includes('csrf')) {
        return '安全校验失败（CSRF），请刷新页面后重试'
      }
      return '权限不足：仅主管理员可触发测试邮件'
    }
    return '测试邮件发送失败，请稍后重试'
  }
  return '发送测试邮件时发生网络错误，请稍后重试'
}

/** 测试 Webhook 外落按钮旁的失败文案；已知状态中文话术，其余兜底中文。 */
export function formatWebhookTestError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return '登录已过期，请重新登录后再试'
    }
    if (error.status === 409) {
      return '无法发送测试 Webhook：推送开关未开启或未配置 Secret'
    }
    if (error.status === 429) {
      const wait = extractRetryAfterSeconds(error)
      return wait ? `Webhook 测试冷却中，请在 ${wait} 秒后重试` : 'Webhook 测试冷却中，请稍后重试'
    }
    if (error.status === 502) {
      return 'Webhook 测试投递失败，请检查出口地址与网络响应'
    }
    if (error.status === 403) {
      return '权限不足：仅主管理员可触发 Webhook 测试'
    }
    return 'Webhook 测试发送失败，请稍后重试'
  }
  return '发送测试 Webhook 时发生网络错误，请稍后重试'
}
