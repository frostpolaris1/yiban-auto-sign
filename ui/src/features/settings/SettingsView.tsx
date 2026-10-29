/**
 * **功能**
 * 账号设置页：账号信息卡与修改密码卡。
 *
 * **归属**
 * `features/settings` 展示层：页面装配，域逻辑在 `change-password.ts`。
 *
 * **口径**
 * 会话由壳保证：`useMe` 与壳读同一份 `['me']` 缓存，不额外请求。
 * 两卡并排，窄屏自动叠成一列（`.grid` 与 `.col-6` 的既有行为）。
 * 所有角色都能进本页：改密端点对三种角色开放。
 */

import { PageHeader } from '@/components/ui/PageHeader'
import { useMe } from '@/lib/session'
import { AccountInfoCard } from './AccountInfoCard'
import { ChangePasswordCard } from './ChangePasswordCard'

export function SettingsView() {
  const meQuery = useMe()
  const me = meQuery.data

  // 壳在会话解析完成前不渲染出口；这里只防御缓存被清空的瞬间。
  if (!me) return null

  return (
    <>
      <PageHeader
        eyebrow="系统 · 账号设置"
        title={
          <>
            账号<span className="accent">设置</span>
          </>
        }
        subtitle="查看当前账号信息，并修改登录密码。"
      />

      <div className="grid">
        <AccountInfoCard me={me} className="col-6" />
        <ChangePasswordCard className="col-6" />
      </div>
    </>
  )
}
