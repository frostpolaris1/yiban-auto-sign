/**
 * **功能**
 * 账号页的权限谓词与置灰原因。
 *
 * **归属**
 * `features/accounts` 纯逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 角色集合以契约各端点的 `x-roles` 为准：
 * verify 与 review 限 master/admin；sign 对全体开放。
 * 学校筛选只对能读 `GET /api/schools` 的角色开放。
 * 暂停与恢复是 (角色, 状态) 二元谓词：
 * 暂停只从 active 出发；恢复只认匹配的暂停种类。
 * 无会话角色时一律禁用，原因与「有角色但权限不足」分开写。
 */

import type { AccountStatus } from '../../lib/api/account'
import type { Role } from '../../lib/api/auth'

export type AccountAction = 'sign' | 'verify' | 'review' | 'schoolFilter'

/** 行内写操作的两种暂停方向：暂停与恢复共用一个端点。 */
export type AccountActionKind = 'pause' | 'resume' | 'sign' | 'verify' | 'review'

const ALLOWED: Record<AccountAction, readonly Role[]> = {
  sign: ['master', 'admin', 'user'],
  verify: ['master', 'admin'],
  review: ['master', 'admin'],
  schoolFilter: ['master', 'admin'],
}

export function can(role: Role | null | undefined, action: AccountAction): boolean {
  if (!role) return false
  return ALLOWED[action].includes(role)
}

/** 允许时返回 null；拒绝时返回一句可见原因。 */
export function denyReason(
  role: Role | null | undefined,
  action: AccountAction,
): string | null {
  if (can(role, action)) return null
  if (!role) return '会话角色未识别'
  return '需要管理员权限'
}

export type PauseDirection = 'pause' | 'resume'

/** 已暂停的两态显示恢复；其余状态显示暂停。 */
export function pauseDirection(status: AccountStatus): PauseDirection {
  return status === 'paused_admin' || status === 'paused_user' ? 'resume' : 'pause'
}

/** 恢复只认匹配的种类：管理员恢复 paused_admin，用户恢复 paused_user。 */
const RESUME_ROLES: Partial<Record<AccountStatus, readonly Role[]>> = {
  paused_admin: ['master', 'admin'],
  paused_user: ['user'],
}

export function canChangePause(
  role: Role | null | undefined,
  status: AccountStatus,
): boolean {
  if (!role) return false
  if (status === 'active') return true
  return RESUME_ROLES[status]?.includes(role) ?? false
}

export function pauseDenyReason(
  role: Role | null | undefined,
  status: AccountStatus,
): string | null {
  if (canChangePause(role, status)) return null
  if (!role) return '会话角色未识别'
  if (pauseDirection(status) === 'resume') return '该暂停种类只能由对应角色恢复'
  return '仅正常状态可暂停'
}

export type RowMenuItem = 'edit' | 'location' | 'delete'

/** 行菜单各项的端点角色：PATCH 与 DELETE 对全体开放，location 只归 user/admin。 */
const MENU_ALLOWED: Record<RowMenuItem, readonly Role[]> = {
  edit: ['master', 'admin', 'user'],
  location: ['user', 'admin'],
  delete: ['master', 'admin', 'user'],
}

export function canUseMenu(role: Role | null | undefined, item: RowMenuItem): boolean {
  if (!role) return false
  return MENU_ALLOWED[item].includes(role)
}

export function menuDenyReason(
  role: Role | null | undefined,
  item: RowMenuItem,
): string | null {
  if (canUseMenu(role, item)) return null
  if (!role) return '会话角色未识别'
  return '仅 user/admin 可设置'
}
