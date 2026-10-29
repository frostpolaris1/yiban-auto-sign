/**
 * **功能**
 * 会话身份的展示取值：角色文案、用户状态文案、头像缩写、显示名。
 *
 * **归属**
 * `lib` 工具层：纯函数，壳与页面共用一份。
 *
 * **口径**
 * 角色取值域是契约 `UserRole`：`master | admin | user`。
 * 用户状态取值域是契约 `UserStatus`：`active | disabled`。
 * 一词一义：全仓只此一份角色文案与状态文案。
 * `active`、`disabled` 的文案同时供用户、学校与账号三处状态表引用。
 * 头像取邮箱 local part 前两个字符；没有 local part 时退回邮箱首字符。
 */

import type { Role, UserStatus } from './api/auth'

const ROLE_LABELS: Record<Role, string> = {
  master: '超级管理员',
  admin: '管理员',
  user: '用户',
}

/**
 * 状态值 `active` 的共用文案。
 * 用户状态、学校状态与账号状态都用这一个词；三处不得各写一份。
 */
export const ACTIVE_STATUS_LABEL = '正常'

/** 状态值 `disabled` 的共用文案。用户状态与学校状态共用。 */
export const DISABLED_STATUS_LABEL = '停用'

const USER_STATUS_LABELS: Record<UserStatus, string> = {
  active: ACTIVE_STATUS_LABEL,
  disabled: DISABLED_STATUS_LABEL,
}

export function roleLabel(role: Role): string {
  return ROLE_LABELS[role]
}

/** 用户状态文案。与学校状态的同一组词一致：`正常`、`停用`。 */
export function userStatusLabel(status: UserStatus): string {
  return USER_STATUS_LABELS[status]
}

/** 邮箱 local part。`operator@example.com` → `operator`。 */
export function displayNameFor(email: string): string {
  const local = email.split('@')[0]
  return local ? local : email
}

export function initialsFor(email: string): string {
  const name = displayNameFor(email)
  return name.slice(0, 2).toUpperCase()
}
