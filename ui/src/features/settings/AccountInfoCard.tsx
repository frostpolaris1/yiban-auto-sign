/**
 * **功能**
 * 账号信息卡：邮箱、角色、状态。
 *
 * **归属**
 * `features/settings` 展示层：只读会话对象，不发请求。
 *
 * **口径**
 * 三个字段全部来自 `GET /api/auth/me` 的 `SessionUser`。
 * 该响应没有「上次改密时间」，故卡片不显示它；界面不得自造后端字段。
 * 邮箱与角色由壳传入，不在这里再取一次会话。
 */

import { Card } from '@/components/ui/Card'
import type { SessionUser } from '@/lib/api/auth'
import { roleLabel, userStatusLabel } from '@/lib/identity'

type AccountInfoCardProps = {
  me: SessionUser
  className?: string
}

export function AccountInfoCard({ me, className }: AccountInfoCardProps) {
  return (
    <Card
      className={className}
      eyebrow="Account"
      title="账号信息"
      action={<span className="app-caption">只读</span>}
    >
      <p className="app-muted">以下信息来自当前会话。邮箱与角色由管理员在创建时设定。</p>
      <dl className="app-detail-list">
        <dt>邮箱</dt>
        <dd>{me.email}</dd>
        <dt>角色</dt>
        <dd>{roleLabel(me.role)}</dd>
        <dt>状态</dt>
        <dd>{userStatusLabel(me.status)}</dd>
      </dl>
    </Card>
  )
}
