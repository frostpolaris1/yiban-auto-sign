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
import { AccountPhotos } from '@/features/photos/AccountPhotos'
import { SHOWCASE, useMe } from '@/lib/session'

export function AccountsPage() {
  const me = useMe()
  const role = me.data?.role ?? null
  // 契约：/api/accounts/{id}/photos 归 user 与 admin；master 不在契约内。
  // 展示构建没有后端，保留照片区以便看样式。
  const canViewPhotos = SHOWCASE || role === 'user' || role === 'admin'
  return (
    <AccountsView
      photoSection={
        canViewPhotos
          ? (account) => (
              <AccountPhotos
                accountId={account.id}
                schoolId={account.school_id}
                ownerUserId={account.owner_user_id}
              />
            )
          : undefined
      }
    />
  )
}
