/**
 * **功能**
 * 用户端设置路由页：只装配 `features/me` 的设置容器。
 *
 * **归属**
 * `pages/user-app` 路由页：不承载保存与危险操作逻辑。
 *
 * **口径**
 * `/app/settings` 是二级页；底栏仍高亮设置。
 */

import { SettingsView } from '@/features/me/SettingsView'

export function UserSettingsPage() {
  return <SettingsView />
}
