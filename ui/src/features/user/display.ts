/**
 * **功能**
 * 用户角色的下拉选项、用户状态的文案与徽章色。
 *
 * **归属**
 * `features/user` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 角色标签与状态文案取 `lib/identity` 的唯一一份，本文件不重写。
 * 状态值只取自契约 `UserStatus`：`active | disabled`。
 * 角色选项的顺序固定为 master、admin、user。
 * 时间格式化在 `lib/format`，与账号页、学校页共用一份。
 */

import type { Tone } from '@/components/ui/Badge'
import type { UserRole, UserStatus } from '@/lib/api/user'
import { ACTIVE_STATUS_LABEL, DISABLED_STATUS_LABEL, roleLabel } from '@/lib/identity'

/** 下拉选项与行内标签同源，避免两处文案漂移。 */
export const USER_ROLE_OPTIONS: Array<{ value: UserRole; label: string }> = (
  ['master', 'admin', 'user'] as const
).map((value) => ({ value, label: roleLabel(value) }))

/** 状态文案引用 `lib/identity` 的共用词，不在这里再写一遍。 */
const STATUS_LABELS: Record<UserStatus, string> = {
  active: ACTIVE_STATUS_LABEL,
  disabled: DISABLED_STATUS_LABEL,
}

export const USER_STATUS_OPTIONS: Array<{ value: UserStatus; label: string }> = (
  Object.keys(STATUS_LABELS) as UserStatus[]
).map((value) => ({ value, label: STATUS_LABELS[value] }))

const STATUS_TONES: Record<UserStatus, Tone> = {
  active: 'success',
  disabled: 'warning',
}

export function userStatusLabel(status: UserStatus): string {
  return STATUS_LABELS[status]
}

export function userStatusTone(status: UserStatus): Tone {
  return STATUS_TONES[status]
}
