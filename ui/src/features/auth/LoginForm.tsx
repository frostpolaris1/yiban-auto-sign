/**
 * **功能**
 * 静态登录表单：邮箱、密码、提交按钮。
 *
 * **归属**
 * `features/auth` 域组件。
 *
 * **口径**
 * 本批不调端点，不存凭据。
 * 提交只做 `preventDefault`，页面不跳转。
 * **不得添加"保持登录"选项**。会话期限由服务端定。
 */

import { Icon } from '@/components/ui/Icon'
import { TextInput } from '@/components/ui/Input'

/**
 * 字段逐个照 Adminator `src/signin.html`。
 * 提交停在页面边界：不发请求，不触发导航。
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
