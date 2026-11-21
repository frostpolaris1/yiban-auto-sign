/**
 * **功能**
 * 账号状态与今日结果的纯展示映射：管理端与用户端共用一张表。
 *
 * **归属**
 * `lib` 工具层：无 React、无网络、无组件 import。
 *
 * **口径**
 * 状态值只取自契约 `AccountStatus`；管理端文案与用户端文案分开，
 * 避免把「管理员暂停」这种后台词带进用户 App。
 * 结果值只取自诊断 `state` 的闭集；未知值不猜原因，交给调用方。
 */

import type { AccountStatus } from './api/account'
import { ACTIVE_STATUS_LABEL } from './identity'

/** 徽章色的子集；与 `components/ui/Badge` 的 `Tone` 结构兼容。 */
export type AccountStatusTone = 'success' | 'warning' | 'danger' | 'info'

/** 管理端标签：保持既有口径。 */
export const ACCOUNT_STATUS_LABELS: Record<AccountStatus, string> = {
  pending: '待审核',
  active: ACTIVE_STATUS_LABEL,
  rejected: '已驳回',
  paused_admin: '管理员暂停',
  paused_user: '用户暂停',
  credential_error: '凭据异常',
}

/** 用户端标签：暂停不区分管理员/用户，原因放详情。 */
export const USER_ACCOUNT_STATUS_LABELS: Record<AccountStatus, string> = {
  pending: '待审核',
  active: '正常',
  rejected: '已驳回',
  paused_admin: '已暂停',
  paused_user: '已暂停',
  credential_error: '凭据异常',
}

export const ACCOUNT_STATUS_OPTIONS: Array<{ value: AccountStatus; label: string }> = (
  Object.keys(ACCOUNT_STATUS_LABELS) as AccountStatus[]
).map((value) => ({ value, label: ACCOUNT_STATUS_LABELS[value] }))

const TONES: Record<AccountStatus, AccountStatusTone> = {
  pending: 'warning',
  active: 'success',
  rejected: 'danger',
  paused_admin: 'info',
  paused_user: 'info',
  credential_error: 'danger',
}

export function statusLabel(status: AccountStatus): string {
  return ACCOUNT_STATUS_LABELS[status]
}

export function userStatusLabel(status: AccountStatus, deleted = false): string {
  if (deleted) return '已删除'
  return USER_ACCOUNT_STATUS_LABELS[status]
}

export function statusTone(status: AccountStatus): AccountStatusTone {
  return TONES[status]
}

export function userStatusTone(status: AccountStatus, deleted = false): AccountStatusTone {
  if (deleted) return 'info'
  return TONES[status]
}

export function isPending(status: AccountStatus): boolean {
  return status === 'pending'
}

/** 行菜单与编辑模态的标题文案；被拒账号的编辑即重新提交。 */
export function editActionLabel(status: AccountStatus): string {
  return status === 'rejected' ? '修改并重新提交' : '编辑'
}

/**
 * 今日状态闭集。`null` 表示今天没有任务行。
 * 契约 `state` 是 string；未知值落到 `null`，不猜。
 */
export type TodayState = 'pending' | 'running' | 'success' | 'failed' | 'skipped' | null

const TODAY_STATES = new Set(['pending', 'running', 'success', 'failed', 'skipped'])

export function parseTodayState(value: string | null | undefined): TodayState {
  if (!value) return null
  return TODAY_STATES.has(value) ? (value as TodayState) : null
}

export function todayStateLabel(state: TodayState): string {
  switch (state) {
    case 'pending':
      return '待执行'
    case 'running':
      return '签到中'
    case 'success':
      return '成功'
    case 'failed':
      return '未成功'
    case 'skipped':
      return '未执行'
    default:
      return '无任务'
  }
}

/**
 * 失败建议的人话。`suggestion` 是闭集；未知值不编原因。
 * 文案只描述下一步，不冒充平台原因。
 */
export type SignSuggestion = 'wait' | 'retry_now' | 're_arm' | 'contact_admin'

export function suggestionText(suggestion: string | null | undefined): string {
  switch (suggestion) {
    case 'retry_now':
      return '现在可以再试一次。'
    case 'wait':
      return '系统会自动再试一次。'
    case 're_arm':
      return '系统会在下一个窗口再试一次。'
    case 'contact_admin':
      return '请联系管理员处理。'
    default:
      return '请稍后查看结果。'
  }
}

/** 备注名优先；为空时回退「账号 + 手机尾 4 位」，不显示 UUID。 */
export function displayName(name: string, phoneMasked: string): string {
  const trimmed = name.trim()
  if (trimmed !== '') return trimmed
  const digits = phoneMasked.replace(/\D/g, '')
  return `账号 ${digits.slice(-4) || '----'}`
}
