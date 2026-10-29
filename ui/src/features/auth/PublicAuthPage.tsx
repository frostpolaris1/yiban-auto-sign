/**
 * **功能**
 * 公开认证页的外壳：会话守卫、左侧介绍面板、右侧卡片位。
 *
 * **归属**
 * `features/auth` 域组件：只排布，只读会话。
 *
 * **口径**
 * 会话解析中显示整页加载态，不先闪表单。
 * 已登录访问送回 `/`：这两页只对未登录者有意义。
 * 登录页不走本组件：它有自己的提交锁。
 */

import type { ReactNode } from 'react'
import { Navigate } from '@tanstack/react-router'
import { SessionLoading } from '@/components/ui/SessionLoading'
import { useMe } from '@/lib/session'
import { AuthAside } from './AuthAside'

type PublicAuthPageProps = {
  children: ReactNode
}

export function PublicAuthPage({ children }: PublicAuthPageProps) {
  const meQuery = useMe()

  if (meQuery.isPending) return <SessionLoading />
  if (meQuery.data) return <Navigate to="/" replace />

  return (
    <div className="auth-shell">
      <AuthAside />
      <main className="auth-main">
        <div className="auth-card">{children}</div>
      </main>
    </div>
  )
}
