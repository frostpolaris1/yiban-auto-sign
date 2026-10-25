/**
 * **功能**
 * 登录页：左侧介绍面板，右侧表单。
 *
 * **归属**
 * `pages` 路由页：只装配，不承载域逻辑。
 *
 * **口径**
 * 静态页。不发请求，不存凭据。
 */

import { Link } from '@tanstack/react-router'
import { AuthAside } from '@/features/auth/AuthAside'
import { LoginForm } from '@/features/auth/LoginForm'

/** 只渲染界面。接入认证前，提交不会离开本页。 */
export function LoginPage() {
  return (
    <div className="auth-shell">
      <AuthAside />
      <main className="auth-main">
        <div className="auth-main-top">
          <span className="switch-link">
            静态演示 · <Link to="/">返回控制台</Link>
          </span>
        </div>
        <LoginForm />
      </main>
    </div>
  )
}
