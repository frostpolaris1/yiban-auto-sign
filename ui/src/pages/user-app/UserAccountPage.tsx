/**
 * **功能**
 * 用户端账号详情路由页：只装配 `features/me` 的账号容器。
 *
 * **归属**
 * `pages/user-app` 路由页：不承载取数与动作逻辑。
 *
 * **口径**
 * `/app/account` 是二级页；底栏仍高亮主页。
 */

import { AccountView } from '@/features/me/AccountView'

export function UserAccountPage() {
  return <AccountView />
}
