import { Link } from '@tanstack/react-router'
import { AuthAside } from '@/features/auth/AuthAside'
import { LoginForm } from '@/features/auth/LoginForm'

/**
 * Login page.
 * Static only: no endpoint call, no credential storage.
 */
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
