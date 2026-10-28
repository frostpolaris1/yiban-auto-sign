/**
 * **功能**
 * 账号管理页：只挂载账号域容器。
 *
 * **归属**
 * `pages` 路由页：页面逻辑留在 `features/accounts`。
 *
 * **口径**
 * 路径 `/accounts` 与 `nav.ts` 的叶子一一对应。
 */

import { AccountsView } from '@/features/accounts/AccountsView'

export function AccountsPage() {
  return <AccountsView />
}
