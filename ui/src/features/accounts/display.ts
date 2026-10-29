/**
 * **功能**
 * 账号状态的文案与徽章色。
 *
 * **归属**
 * `features/accounts` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 状态值只取自契约 `AccountStatus`；`active` 的词引用 `lib/identity` 的共用状态词。
 * 其余状态文案只在 `features/accounts` 一张表里。
 * 时间格式化在 `lib/format`，与学校页共用一份。
 */

import type { Tone } from '@/components/ui/Badge'
import type { AccountStatus } from '@/lib/api/account'
import { ACTIVE_STATUS_LABEL } from '@/lib/identity'

const LABELS: Record<AccountStatus, string> = {
  pending: '待审核',
  active: ACTIVE_STATUS_LABEL,
  rejected: '已驳回',
  paused_admin: '管理员暂停',
  paused_user: '用户暂停',
  credential_error: '凭据异常',
}

/** 下拉选项与标签同源，避免两处文案漂移。 */
export const ACCOUNT_STATUS_OPTIONS: Array<{ value: AccountStatus; label: string }> = (
  Object.keys(LABELS) as AccountStatus[]
).map((value) => ({ value, label: LABELS[value] }))

const TONES: Record<AccountStatus, Tone> = {
  pending: 'warning',
  active: 'success',
  rejected: 'danger',
  paused_admin: 'info',
  paused_user: 'info',
  credential_error: 'danger',
}

export function statusLabel(status: AccountStatus): string {
  return LABELS[status]
}

export function statusTone(status: AccountStatus): Tone {
  return TONES[status]
}

export function isPending(status: AccountStatus): boolean {
  return status === 'pending'
}
