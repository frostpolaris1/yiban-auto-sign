/**
 * **功能**
 * 用户端签到时间偏好深链：设置页＋自动打开时间偏好弹窗。
 *
 * **归属**
 * `pages/user-app` 路由页：只装配 `features/me` 的设置容器。
 *
 * **口径**
 * 子表单不再独占整页（弹窗式表单优先）；本路由仅保留深链与测试入口，
 * 关闭弹窗后 URL 归位 /app/settings。取数与保存在 `TimePrefDialog`。
 */

import { SettingsView } from '@/features/me/SettingsView'

export function UserTimePrefPage() {
  return <SettingsView initialDialog="time-pref" />
}