/**
 * **功能**
 * 公开认证页外壳：左侧介绍、顶栏、居中的表单卡、底部项目链接。
 *
 * **归属**
 * `features/auth` 域组件：只排布，不承载会话守卫与域逻辑。
 *
 * **口径**
 * 登录、忘记密码、重置密码三页共用，避免三套外壳漂移。
 * 窄屏隐藏左栏后，顶栏补回品牌与主题开关。
 * 法务链接暂时指向仓库文档：新服务端还没有 /terms 与 /privacy 路由。
 */

import type { ReactNode } from 'react'
import { BrandMark } from '@/components/ui/BrandMark'
import { Icon } from '@/components/ui/Icon'
import { useTheme } from '@/lib/theme'
import { APP_VERSION } from '@/lib/version'
import { AuthAside } from './AuthAside'

const REPO_URL = 'https://github.com/frostpolaris1/yiban-auto-sign'
const DOCS_URL = `${REPO_URL}/blob/main`

/** 窄屏品牌兜底：左栏在 ≤900px 时整块隐藏。 */
function AuthTopBar() {
  const { theme, toggleTheme } = useTheme()
  const nextTheme = theme === 'dark' ? '浅色' : '深色'

  return (
    <div className="auth-main-top">
      <div className="auth-mobile-brand">
        <span className="logo">
          <BrandMark />
        </span>
        <span>易班自动签到</span>
      </div>
      <button
        type="button"
        className="app-theme-toggle"
        aria-label={`切换到${nextTheme}主题`}
        title={`切换到${nextTheme}主题`}
        onClick={toggleTheme}
      >
        <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} />
      </button>
    </div>
  )
}

/** 页脚只放可验证的事实与出口，不放备案占位或假徽章。 */
function AuthFooter() {
  return (
    <div className="auth-main-bottom">
      <div className="app-auth-footer-line">易班自动签到 · v{APP_VERSION} · AGPL-3.0</div>
      <nav className="app-auth-links" aria-label="项目与法务链接">
        <a href={REPO_URL} target="_blank" rel="noopener noreferrer">
          GitHub
        </a>
        <span aria-hidden="true">·</span>
        <a href={`${DOCS_URL}/USER_AGREEMENT.md`} target="_blank" rel="noopener noreferrer">
          用户协议
        </a>
        <span aria-hidden="true">·</span>
        <a href={`${DOCS_URL}/PRIVACY_POLICY.md`} target="_blank" rel="noopener noreferrer">
          隐私政策
        </a>
      </nav>
    </div>
  )
}

export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="auth-shell">
      <AuthAside />
      <main className="auth-main">
        <AuthTopBar />
        <div className="auth-main-center">
          <div className="auth-card">{children}</div>
        </div>
        <AuthFooter />
      </main>
    </div>
  )
}
