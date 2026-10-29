/**
 * **功能**
 * 页面级的角色准入表：哪些角色能看见并进入哪些模块。
 *
 * **归属**
 * `lib` 工具层：`components/shell` 的导航与 `features/*` 的页面共用一份。
 *
 * **口径**
 * 整页/模块级无权限＝导航项不渲染（2026-10-28 用户裁决）。
 * 页内动作级无权限仍按「置灰不隐藏」处理，其谓词留在各 feature。
 * 本表只登记契约 `x-roles` 的并集，不写页面文案。
 */

import type { Role } from './api/auth'

/** `GET /api/schools` 的 `x-roles`：master 看全部，admin 看绑定范围。 */
export const SCHOOL_VIEW_ROLES: readonly Role[] = ['master', 'admin']

/** `/api/schedule/*` 与 `/api/capacity` 的 `x-roles`：时间片是管理页。 */
export const SCHEDULE_VIEW_ROLES: readonly Role[] = ['master', 'admin']

/** `GET /api/users` 的 `x-roles`：用户管理只归 master。 */
export const USER_VIEW_ROLES: readonly Role[] = ['master']

/** 角色为 null（会话未识别）时一律拒绝。 */
export function roleAllowed(role: Role | null | undefined, allowed: readonly Role[]): boolean {
  return role ? allowed.includes(role) : false
}
