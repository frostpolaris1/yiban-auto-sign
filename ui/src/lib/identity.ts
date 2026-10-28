/**
 * **功能**
 * 会话身份的展示取值：角色文案、头像缩写、显示名。
 *
 * **归属**
 * `lib` 工具层：纯函数，壳与页面共用一份。
 *
 * **口径**
 * 角色取值域是契约 `UserRole`：`master | admin | user`。
 * 一词一义：全仓只此一份角色文案。
 * 头像取邮箱 local part 前两个字符；没有 local part 时退回邮箱首字符。
 */

import type { Role } from './api/auth'

const ROLE_LABELS: Record<Role, string> = {
  master: '超级管理员',
  admin: '管理员',
  user: '用户',
}

export function roleLabel(role: Role): string {
  return ROLE_LABELS[role]
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
