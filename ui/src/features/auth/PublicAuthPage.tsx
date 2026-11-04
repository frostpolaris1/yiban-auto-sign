/**
 * **功能**
 * 公开认证页的会话守卫：解析中显示加载态，已登录送回首页。
 *
 * **归属**
 * `features/auth` 域组件：只读会话，排布交给 `AuthLayout`。
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
import { AuthLayout } from './AuthLayout'

type PublicAuthPageProps = {
  children: ReactNode
}

export function PublicAuthPage({ children }: PublicAuthPageProps) {
  const meQuery = useMe()

  if (meQuery.isPending) return <SessionLoading />
  if (meQuery.data) return <Navigate to="/" replace />

  return <AuthLayout>{children}</AuthLayout>
}
