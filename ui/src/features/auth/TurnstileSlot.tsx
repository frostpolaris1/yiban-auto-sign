/**
 * **功能**
 * 注册页的人机验证预留位：给 Cloudflare Turnstile 留出挂载点，并按常规控件 300×65 的
 * 最小版面预留高度；接入前显示一块虚线占位，避免把「还没接」误看成「已在防护」。
 *
 * **归属**
 * `features/auth` 域组件，由 `RegisterForm` 在协议勾选之后、提交按钮之前渲染。
 *
 * **口径**
 * 只占版面，不参与提交校验：`validateRegister` 不认它，`buildRegisterBody` 产出的仍是
 * 契约 `RegisterRequest` 的六个字段。
 * 接入 Turnstile：`turnstile.render('[data-turnstile-slot="register"]', { sitekey, callback })`
 * 渲染进挂载点，回调里的 token 随注册请求回传；后端在
 * `server/internal/entry/http/router/auth_handlers.go` 的 `AuthRegister` 里调 siteverify，
 * 并在 `server/contracts/openapi.yaml` 的 `RegisterRequest` 补字段。
 * 接入时删掉占位块（.app-turnstile-slot-placeholder），挂载点本身保留：
 * 65px 的最小高度写在 .app-turnstile-slot 上，删掉占位不会让提交区跳动。
 */

export function TurnstileSlot() {
  return (
    <div className="app-turnstile-slot" data-turnstile-slot="register">
      {/* 占位块：Turnstile 的 iframe 会由脚本插进上面的挂载点，接入后删掉这里。 */}
      <div className="app-turnstile-slot-placeholder">
        <span className="app-turnstile-slot-label">人机验证占位</span>
        <span className="app-turnstile-slot-hint">Cloudflare Turnstile 预留位</span>
      </div>
    </div>
  )
}
