/**
 * **功能**
 * 用户页的 URL 状态：筛选、含已删除开关与游标互转。
 *
 * **归属**
 * `features/user` 纯逻辑：无 React、无网络、无 DOM。
 * `app/router.tsx` 的 `validateSearch` 与 `UsersView` 每次渲染都经过这里，
 * 单元测试直接覆盖。
 *
 * **口径**
 * URL 是唯一事实源：`?role&status&q&group&deleted&cursor`。
 * 空值与缺省值都不产生键，保证同状态同 URL。
 * `deleted` 只有两态：开＝键存在，关＝键不存在。
 * `userSearchRecord` 写 `deleted=1`；路由把校验结果（布尔）再序列化回
 * URL 时显示为 `deleted=true`。解析两者都收，刷新与手写 URL 均可还原。
 * `group` 是待处理/正式/空用户；与 `deleted` 互斥。
 * 游标是服务端的不透明串，原样透传。
 * 筛选变化必须丢游标：`withFilter` 统一清，调用方不再各写一份。
 */

import type { UserGroup, UserRole, UserStatus } from '@/lib/api/user'

export type UserURLState = {
  role?: UserRole
  status?: UserStatus
  q?: string
  group?: UserGroup
  deleted?: boolean
  cursor?: string
}

/** 从路由 search 记录读状态。未知键忽略，值非法时按缺省处理。 */
export function parseUserSearchRecord(search: Record<string, unknown>): UserURLState {
  const state: UserURLState = {}
  const role = search.role
  if (role === 'master' || role === 'admin' || role === 'user') state.role = role
  const status = search.status
  if (status === 'active' || status === 'disabled') state.status = status
  const q = search.q
  if (typeof q === 'string' && q !== '') state.q = q
  const group = search.group
  if (group === 'pending' || group === 'normal' || group === 'vacant') state.group = group
  // 手写 URL 同时带 group 与 deleted 时分组优先：软删行不参与分组。
  if (!state.group && (search.deleted === '1' || search.deleted === 'true')) {
    state.deleted = true
  }
  const cursor = search.cursor
  if (typeof cursor === 'string' && cursor !== '') state.cursor = cursor
  return state
}

/** 把状态写成路由 search 记录。空值与缺省值不产生键。 */
export function userSearchRecord(state: UserURLState): Record<string, string> {
  const record: Record<string, string> = {}
  if (state.role) record.role = state.role
  if (state.status) record.status = state.status
  if (state.q) record.q = state.q
  // 手写状态同时带两者时分组优先，与解析口径一致。
  if (state.group) record.group = state.group
  else if (state.deleted) record.deleted = '1'
  if (state.cursor) record.cursor = state.cursor
  return record
}

/** 换筛选：统一回到第一页。游标只在显式翻页时存在。 */
export function withFilter(
  state: UserURLState,
  patch: Partial<Omit<UserURLState, 'cursor'>>,
): UserURLState {
  const next: UserURLState = { ...state, ...patch }
  delete next.cursor
  if (!next.q) delete next.q
  if (!next.group) delete next.group
  if (!next.deleted) delete next.deleted
  return next
}

/** 选分组：分组视图只数未删用户，故同时退出已删除视图。 */
export function withGroup(state: UserURLState, group?: UserGroup): UserURLState {
  const next = withFilter(state, { group })
  delete next.deleted
  return next
}

/** 切已删除视图：软删行不参与分组，故同时清掉分组。 */
export function withDeleted(state: UserURLState, deleted: boolean): UserURLState {
  const next = withFilter(state, { deleted })
  delete next.group
  return next
}
