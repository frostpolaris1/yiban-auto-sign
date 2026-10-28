/**
 * **功能**
 * 登录页左侧介绍面板：品牌、标语、页脚。
 *
 * **归属**
 * `features/auth` 域组件。
 *
 * **口径**
 * 纯文案，不发请求。
 * 不承诺会话期限，也不描述登录后的具体行为。
 */

import { BrandMark, Icon } from '@/components/ui/Icon'

/**
 * 结构与类名照 Adminator `src/signin.html`。
 * 文案只陈述产品能力，不再声明「未接后端」。
 */
export function AuthAside() {
  return (
    <aside className="auth-aside">
      <div className="auth-brand">
        <div className="logo">
          <BrandMark />
        </div>
        <div className="name">易班自动签到</div>
      </div>

      <div className="auth-aside-body">
        <span className="auth-aside-eyebrow">账号控制台</span>
        <h1>登录后管理账号与签到。</h1>
        <p>登录状态由服务端管理。</p>
        <div className="auth-quote">
          设计令牌来自 Adminator 4。明暗两套取值与上游一致。
          <div className="auth-quote-author">
            <div className="av">UI</div>
            <div>易班自动签到 · 控制台</div>
          </div>
        </div>
      </div>

      <div className="auth-aside-footer">
        <span>2026</span>
        <span>ADMINATOR 4</span>
        <Icon name="external" size={11} />
      </div>
    </aside>
  )
}
