/**
 * **功能**
 * 设置页路由页：只挂载设置域容器。
 *
 * **归属**
 * `pages` 路由页：页面逻辑留在 `features/settings`。
 *
 * **口径**
 * 路径 `/settings` 与 `nav.ts` 的叶子一一对应。
 * 壳内路由：未登录由壳的守卫落到登录页，本页不另做守卫。
 */

import { SettingsView } from '@/features/settings/SettingsView'

export function SettingsPage() {
  return <SettingsView />
}
