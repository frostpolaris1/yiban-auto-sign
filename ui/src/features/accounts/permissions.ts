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
 * 展示构建（`VITE_SHOWCASE=1`）只读：全部写动作返回同一条原因，
 * 该原因优先于角色与状态判断；读动作（schoolFilter）不受影响。
 */

import type { AccountStatus } from '../../lib/api/account'
import type { Role } from '../../lib/api/auth'
import { READ_ONLY_REASON, SHOWCASE } from '../../lib/showcase-mode'

export type AccountAction = 'sign' | 'verify' | 'review' | 'schoolFilter' | 'create' | 'restore' | 'deviceRefresh'

/** 行内写操作的两种暂停方向：暂停与恢复共用一个端点。 */
export type AccountActionKind = 'pause' | 'resume' | 'sign' | 'verify' | 'review'

/** 写动作清单。展示构建下这些一律禁用；读动作不在其中。 */
const WRITE_ACTIONS: readonly AccountAction[] = [
  'sign',
  'verify',
  'review',
  'create',
  'restore',
  'deviceRefresh',
]

const ALLOWED: Record<AccountAction, readonly Role[]> = {
  sign: ['master', 'admin', 'user'],
  verify: ['master', 'admin'],
  review: ['master', 'admin'],
  schoolFilter: ['master', 'admin'],
  // UX-2：三角色都可建号与刷新设备；恢复只归 master/admin（8v8）。
  create: ['master', 'admin', 'user'],
  deviceRefresh: ['master', 'admin', 'user'],
  restore: ['master', 'admin'],
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
  if (SHOWCASE && WRITE_ACTIONS.includes(action)) return READ_ONLY_REASON
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
  if (SHOWCASE) return READ_ONLY_REASON
  if (canChangePause(role, status)) return null
  if (!role) return '会话角色未识别'
  if (pauseDirection(status) === 'resume') return '该暂停种类只能由对应角色恢复'
  return '仅正常状态可暂停'
}

export type RowMenuItem = 'edit' | 'location' | 'delete' | 'restore'

/** 行菜单各项的端点角色：PATCH 与 DELETE 对全体开放，location 只归 user/admin，恢复归 master/admin。 */
const MENU_ALLOWED: Record<RowMenuItem, readonly Role[]> = {
  edit: ['master', 'admin', 'user'],
  location: ['user', 'admin'],
  delete: ['master', 'admin', 'user'],
  restore: ['master', 'admin'],
}

export function canUseMenu(role: Role | null | undefined, item: RowMenuItem): boolean {
  if (!role) return false
  return MENU_ALLOWED[item].includes(role)
}

export function menuDenyReason(
  role: Role | null | undefined,
  item: RowMenuItem,
): string | null {
  if (SHOWCASE) return READ_ONLY_REASON
  if (canUseMenu(role, item)) return null
  if (!role) return '会话角色未识别'
  if (item === 'restore') return '需要管理员权限'
  return '仅 user/admin 可设置'
}
