/**
 * **功能**
 * 登录页左侧介绍面板：品牌、标语、页脚。
 *
 * **归属**
 * `features/auth` 域组件。
 *
 * **口径**
 * 纯文案。不发请求，不读会话。
 */

import { BrandMark, Icon } from '@/components/ui/Icon'

/**
 * 结构与类名照 Adminator `src/signin.html`。
 * 文案是 P1 骨架说明。接入认证时整段改写。
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
        <span className="auth-aside-eyebrow">P1 · 前端骨架</span>
        <h1>先固定视觉语言，再接入业务。</h1>
        <p>此页只渲染界面，不发送请求。</p>
        <div className="auth-quote">
          设计令牌来自 Adminator 4。明暗两套取值与上游一致。
          <div className="auth-quote-author">
            <div className="av">UI</div>
            <div>前端批次 · P1B</div>
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
