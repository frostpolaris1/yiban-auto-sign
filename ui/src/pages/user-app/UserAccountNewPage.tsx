/**
 * **功能**
 * 用户端新建账号路由页：只装配 `features/me` 的新建容器。
 *
 * **归属**
 * `pages/user-app` 路由页：不承载取数与提交流程。
 *
 * **口径**
 * `/app/account/new` 是二级页；detect-first 三步由 `AccountNewView` 管。
 */

import { AccountNewView } from '@/features/me/AccountNewView'

export function UserAccountNewPage() {
  return <AccountNewView />
}
