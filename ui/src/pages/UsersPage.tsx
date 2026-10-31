/**
 * **功能**
 * 用户管理页：把 URL search 与写回回调传给用户域容器。
 *
 * **归属**
 * `pages` 路由页：页面逻辑留在 `features/user`。
 *
 * **口径**
 * 路径 `/users` 与 `nav.ts` 的叶子一一对应。
 * 非 master 直接访问时由容器渲染无权限卡片，不发列表请求。
 * search 的解析与序列化都在 `features/user/urlState.ts`。
 */

import { UsersView } from '@/features/user/UsersView'
import type { UserURLState } from '@/features/user/urlState'

type UsersPageProps = {
  search: UserURLState
  onSearchChange: (next: UserURLState, opts?: { replace?: boolean }) => void
}

export function UsersPage({ search, onSearchChange }: UsersPageProps) {
  return <UsersView urlState={search} onSearchChange={onSearchChange} />
}
