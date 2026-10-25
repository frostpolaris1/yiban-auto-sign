import { Icon } from '@/components/ui/Icon'
import { TextInput } from '@/components/ui/Input'

/**
 * Static login form.
 *
 * It mirrors Adminator `src/signin.html` field for field. The submit handler
 * stops at the page boundary: this batch does not call an endpoint and does not
 * store credentials.
 */
export function LoginForm() {
  return (
    <div className="auth-card">
      <h2>登录</h2>
      <p className="sub">本页为静态表单，不连接后端。提交后停留在本页。</p>

      <form className="auth-form" onSubmit={(event) => event.preventDefault()}>
        <TextInput
          id="login-email"
          label="邮箱"
          type="email"
          icon="mail"
          placeholder="name@example.com"
          autoComplete="username"
          required
        />

        <div className="field">
          <div className="field-row">
            <label className="field-label" htmlFor="login-password">
              密码
            </label>
            <span className="app-caption">联系管理员重置</span>
          </div>
          <div className="input-icon">
            <span className="ico">
              <Icon name="shield" />
            </span>
            <input
              id="login-password"
              className="input"
              type="password"
              placeholder="••••••••"
              autoComplete="current-password"
              required
            />
          </div>
        </div>

        <button className="btn btn--primary auth-submit" type="submit">
          登录控制台
          <Icon name="arrowRight" />
        </button>
      </form>

      <div className="auth-main-bottom">
        本批不接认证端点。登录、会话与 CSRF 在 P1 后端批次接入。
      </div>
    </div>
  )
}
