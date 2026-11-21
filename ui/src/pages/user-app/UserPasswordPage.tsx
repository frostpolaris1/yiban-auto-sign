/**
 * **功能**
 * 用户端修改密码深链：设置页＋自动打开改密弹窗。
 *
 * **归属**
 * `pages/user-app` 路由页：只装配 `features/me` 的设置容器。
 *
 * **口径**
 * 子表单不再独占整页（弹窗式表单优先）；本路由仅保留深链与测试入口，
 * 关闭弹窗后 URL 归位 /app/settings。策略与提交在 `PasswordDialog`。
 */

import { SettingsView } from '@/features/me/SettingsView'

export function UserPasswordPage() {
  return <SettingsView initialDialog="password" />
}