/**
 * **功能**
 * 用户管理页：只挂载用户域容器。
 *
 * **归属**
 * `pages` 路由页：页面逻辑留在 `features/user`。
 *
 * **口径**
 * 路径 `/users` 与 `nav.ts` 的叶子一一对应。
 * 非 master 直接访问时由容器渲染无权限卡片，不发列表请求。
 */

import { UsersView } from '@/features/user/UsersView'

export function UsersPage() {
  return <UsersView />
}
