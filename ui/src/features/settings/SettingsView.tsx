/**
 * **功能**
 * 账号设置页（个人域）：账号信息卡、修改密码卡与本人通知订阅矩阵卡。
 *
 * **归属**
 * `features/settings` 展示层：页面装配与页级角色门，域逻辑在 `change-password.ts` 与 `notify-prefs.ts`。
 *
 * **口径**
 * 会话由壳保证：`useMe` 与壳读同一份 `['me']` 缓存，不额外请求。
 * 页级角色门：`SETTINGS_VIEW_ROLES`（master/admin）放行；user 渲染无权限卡片且零业务请求。
 * 信息卡与改密卡并排（`.col-6`），通知订阅卡跨两列（`.col-12`），窄屏自动单列堆叠。
 */

import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import { SETTINGS_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { useMe } from '@/lib/session'
import { AccountInfoCard } from './AccountInfoCard'
import { ChangePasswordCard } from './ChangePasswordCard'
import { NotifyPrefsCard } from './NotifyPrefsCard'

export function SettingsView() {
  const meQuery = useMe()
  const me = meQuery.data

  // 壳在会话解析完成前不渲染出口；这里只防御缓存被清空的瞬间。
  if (!me) return null

  if (!roleAllowed(me.role, SETTINGS_VIEW_ROLES)) {
    return (
      <DeniedCard
        eyebrow="系统 · 账号设置"
        title="无权访问"
        reason="你的角色无权访问账号设置"
      />
    )
  }

  return (
    <>
      <PageHeader
        eyebrow="系统 · 账号设置"
        title={
          <>
            账号<span className="accent">设置</span>
          </>
        }
        subtitle="查看当前账号信息、修改登录密码，并管理本人通知订阅。"
      />

      <div className="grid app-settings-grid">
        <AccountInfoCard me={me} className="col-6" />
        <ChangePasswordCard className="col-6" />
        <NotifyPrefsCard className="col-12" />
      </div>
    </>
  )
}
