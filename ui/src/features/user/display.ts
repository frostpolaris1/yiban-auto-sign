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
import type { UserCounts, UserGroup, UserRole, UserStatus } from '@/lib/api/user'
import { ACTIVE_STATUS_LABEL, DISABLED_STATUS_LABEL, roleLabel } from '@/lib/identity'

/** 下拉选项与行内标签同源，避免两处文案漂移。 */
export const USER_ROLE_OPTIONS: Array<{ value: UserRole; label: string }> = (
  ['master', 'admin', 'user'] as const
).map((value) => ({ value, label: roleLabel(value) }))

/** 已删行的徽标文案：与状态徽标并列展示。 */
export const USER_DELETED_LABEL = '已删除'

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

/** 分组标签。顺序固定：待处理 → 正式 → 空用户。 */
export const USER_GROUP_LABELS: Record<UserGroup, string> = {
  pending: '待处理',
  normal: '正式',
  vacant: '空用户',
}

/**
 * 空分组时的跨组出路：按「先看最近的兄弟组」选第一个有人的组。
 * 没有计数或兄弟组都空时返回 null，不造死链。
 */
export function nextGroupWithMembers(
  group: UserGroup,
  counts: UserCounts | undefined,
): { group: UserGroup; label: string } | null {
  if (!counts) return null
  const order: Record<UserGroup, UserGroup[]> = {
    pending: ['normal', 'vacant'],
    normal: ['vacant', 'pending'],
    vacant: ['normal', 'pending'],
  }
  const memberLabels: Record<UserGroup, string> = {
    pending: '待处理用户',
    normal: '正式用户',
    vacant: '空用户',
  }
  for (const candidate of order[group]) {
    if (counts[candidate] > 0) {
      return { group: candidate, label: `查看${memberLabels[candidate]}` }
    }
  }
  return null
}
