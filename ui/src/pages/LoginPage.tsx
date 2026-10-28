/**
 * **功能**
 * 登录页：左侧介绍面板，右侧表单。
 *
 * **归属**
 * `pages` 路由页：只装配，不承载域逻辑。
 *
 * **口径**
 * 会话解析中显示加载态，不闪表单。
 * 已登录访问本页跳回 `/`。提交期间暂停该兜底守卫、失败复位：
 * 声明式 Navigate 与表单导航没有稳定先后，不加锁会把有效回跳抢成 `/`；
 * 成功路径必然离开本页（登录拼写已全部拦到 `/`），锁随卸载失效。
 */

import { useState } from 'react'
import { Navigate } from '@tanstack/react-router'
import { SessionLoading } from '@/components/ui/SessionLoading'
import { AuthAside } from '@/features/auth/AuthAside'
import { LoginForm } from '@/features/auth/LoginForm'
import { useMe, useSessionExpired } from '@/lib/session'

type LoginPageProps = {
  /** 守卫写入的回跳地址。 */
  redirect?: string
}

export function LoginPage({ redirect }: LoginPageProps) {
  const meQuery = useMe()
  const expired = useSessionExpired()
  // 提交期间暂停兜底守卫：声明式 Navigate 与表单导航没有稳定先后，
  // 会让有效回跳被抢成 `/`。成功后必然离开本页，失败路径由表单复位。
  const [signingIn, setSigningIn] = useState(false)

  if (meQuery.isPending) return <SessionLoading />
  // 已登录不得停在登录页；过期标记除外（等用户重新登录）。
  if (meQuery.data && !expired && !signingIn) return <Navigate to="/" replace />

  return (
    <div className="auth-shell">
      <AuthAside />
      <main className="auth-main">
        <LoginForm
          redirect={redirect}
          expired={expired}
          onSignInStart={() => setSigningIn(true)}
          onSignInFailed={() => setSigningIn(false)}
        />
      </main>
    </div>
  )
}
