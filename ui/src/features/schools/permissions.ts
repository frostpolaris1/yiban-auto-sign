/**
 * **功能**
 * 学校页的权限谓词与置灰原因。
 *
 * **归属**
 * `features/schools` 纯逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 角色集合以契约各端点的 `x-roles` 为准：
 * view 归 master/admin；create、edit、admins 只归 master。
 * 整页 view 的准入与导航共用 `lib/roles` 的一份登记。
 * 页内动作无权限时置灰不隐藏，原因由 `denyReason` 给出。
 */

import type { Role } from '../../lib/api/auth'
import { SCHOOL_VIEW_ROLES, roleAllowed } from '../../lib/roles'

export type SchoolAction = 'view' | 'create' | 'edit' | 'admins'

const ALLOWED: Record<SchoolAction, readonly Role[]> = {
  view: SCHOOL_VIEW_ROLES,
  create: ['master'],
  edit: ['master'],
  admins: ['master'],
}

const DENY_TEXT: Record<SchoolAction, string> = {
  view: '你的角色无权查看学校管理',
  create: '仅 master 可新建学校',
  edit: '仅 master 可编辑',
  admins: '仅 master 可管理学校管理员',
}

export function can(role: Role | null | undefined, action: SchoolAction): boolean {
  return roleAllowed(role, ALLOWED[action])
}

/** 允许时返回 null；拒绝时返回一句可见原因。 */
export function denyReason(role: Role | null | undefined, action: SchoolAction): string | null {
  if (can(role, action)) return null
  if (!role) return '会话角色未识别'
  return DENY_TEXT[action]
}
