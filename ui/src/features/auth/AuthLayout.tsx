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
 * 法务链到服务端 `/terms` 与 `/privacy` 的静态页。
 * 主题切换走整页 root 圆形扩散（探针本质）：天空画布随整页一起被圆揭开。
 * WebGL 入快照靠 preserveDrawingBuffer＋色板变更同步重绘（见 SkyGradient）。
 * 页脚版本行可点：开更新日志弹窗（`GET /api/changelog` 服务端原文）。
 */

import { useState, type ReactNode } from 'react'
import { BrandMark } from '@/components/ui/BrandMark'
import { Icon } from '@/components/ui/Icon'
import { originFromEvent, useTheme } from '@/lib/theme'
import { APP_VERSION } from '@/lib/version'
import { AuthAside } from './AuthAside'
import { ChangelogDialog } from './ChangelogDialog'

const REPO_URL = 'https://github.com/frostpolaris1/yiban-auto-sign'

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
        onClick={(event) => toggleTheme(originFromEvent(event))}
      >
        <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} />
      </button>
    </div>
  )
}

/** 页脚只放可验证的事实与出口，不放备案占位或假徽章。 */
function AuthFooter() {
  const [open, setOpen] = useState(false)
  const [returnFocus, setReturnFocus] = useState<HTMLElement | null>(null)

  return (
    <div className="auth-main-bottom">
      <button
        type="button"
        className="app-auth-version"
        onClick={(event) => {
          setReturnFocus(event.currentTarget)
          setOpen(true)
        }}
      >
        易班自动签到 · v{APP_VERSION} · AGPL-3.0
      </button>
      <nav className="app-auth-links" aria-label="项目与法务链接">
        <a href={REPO_URL} target="_blank" rel="noopener noreferrer">
          GitHub
        </a>
        <span aria-hidden="true">·</span>
        <a href="/terms" target="_blank" rel="noopener noreferrer">
          用户协议
        </a>
        <span aria-hidden="true">·</span>
        <a href="/privacy" target="_blank" rel="noopener noreferrer">
          隐私政策
        </a>
      </nav>
      <ChangelogDialog open={open} onOpenChange={setOpen} returnFocusTo={returnFocus} />
    </div>
  )
}

type AuthLayoutProps = {
  children: ReactNode
  /** 透传给左栏：注册签换三步引导开场，其余页保持能力清单。 */
  asideMode?: 'login' | 'register'
}

export function AuthLayout({ children, asideMode = 'login' }: AuthLayoutProps) {
  return (
    <div className="auth-shell">
      <div className="auth-stage">
        <AuthAside mode={asideMode} />
        <main className="auth-main">
          <a className="uapp-skip" href="#auth-main-card">
            跳转到表单
          </a>
          <AuthTopBar />
          <div className="auth-main-center">
            <section className="auth-card" id="auth-main-card" tabIndex={-1}>
              {children}
            </section>
          </div>
          <AuthFooter />
        </main>
      </div>
    </div>
  )
}
