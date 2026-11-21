/**
 * **功能**
 * 认证页：左侧介绍面板，右侧登录/注册两签。
 *
 * **归属**
 * `pages` 路由页：只装配，不承载域逻辑。
 *
 * **口径**
 * `/login` 与 `/register` 是同一页面的两个签；`/register` 只做深链别名。
 * 会话解析中显示加载态，不闪表单。
 * 已登录访问本页按角色落地：user → `/app`；master/admin → `/`。
 * 提交期间暂停兜底守卫、失败复位；成功后必然离开本页。
 * 不设「恢复已删除账号」入口：软删宽限内直接登录，401 后表单自动走一次恢复再重发。
 * 注册新用户是 `user`，注册即登录后直接进 `/app`。
 */

import { useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Navigate, useNavigate } from '@tanstack/react-router'
import { SessionLoading } from '@/components/ui/SessionLoading'
import { AuthLayout } from '@/features/auth/AuthLayout'
import { LoginForm } from '@/features/auth/LoginForm'
import { RegisterForm } from '@/features/auth/RegisterForm'
import { useRegistrationStatus } from '@/features/auth/useRegistrationStatus'
import { landingForRole, resolveLanding } from '@/lib/landing'
import { SHOWCASE, useMe, useSessionExpired } from '@/lib/session'

export type AuthMode = 'login' | 'register'

type LoginPageProps = {
  /** 守卫写入的回跳地址。注册成功不沿用：新用户先去落地页。 */
  redirect?: string
  /** 当前签；`/register` 路由传 register。 */
  mode?: AuthMode
}

export function LoginPage({ redirect, mode = 'login' }: LoginPageProps) {
  const meQuery = useMe()
  const expired = useSessionExpired()
  const registration = useRegistrationStatus()
  const navigate = useNavigate()
  // 提交期间暂停兜底守卫：声明式 Navigate 与表单导航没有稳定先后，
  // 会让有效回跳被抢成默认落地页。成功后必然离开本页。
  const [signingIn, setSigningIn] = useState(false)

  // 双向交叉横滑：两签常驻一条轨道，切换时轨道平移（旧出新入反向）。
  // 视口高度跟随活跃签的内容高度（注册强度条/错误会动态长高），平滑过渡。
  const loginPaneRef = useRef<HTMLDivElement>(null)
  const registerPaneRef = useRef<HTMLDivElement>(null)
  const loginTabRef = useRef<HTMLButtonElement>(null)
  const registerTabRef = useRef<HTMLButtonElement>(null)
  const [viewportH, setViewportH] = useState<number>()
  useLayoutEffect(() => {
    const active = mode === 'login' ? loginPaneRef.current : registerPaneRef.current
    const inactive = mode === 'login' ? registerPaneRef.current : loginPaneRef.current
    // 非活跃签移出无障碍树与焦点序列，避免 Tab 落到屏外字段。
    if (inactive) inactive.inert = true
    if (!active) return
    active.inert = false
    const measure = () => setViewportH(active.offsetHeight)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(active)
    return () => observer.disconnect()
    // isPending 翻 false 后表单才挂载、ref 才有值，需重跑测量。
  }, [mode, meQuery.isPending])

  if (meQuery.isPending) return <SessionLoading />
  // 展示模式不设登录：静态站没有后端，停在本页只会让人以为要输口令。
  if (SHOWCASE) return <Navigate to="/" replace />
  // 已登录不得停在登录页；按角色落地。
  if (meQuery.data && !expired && !signingIn) {
    return <Navigate to={resolveLanding(meQuery.data.role, redirect)} replace />
  }

  function switchMode(next: AuthMode, focusTab = false): void {
    if (next === mode) return
    void navigate({
      to: next === 'register' ? '/register' : '/login',
      search: redirect ? { redirect } : {},
      replace: true,
    })
    if (focusTab) {
      (next === 'register' ? registerTabRef : loginTabRef).current?.focus()
    }
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>): void {
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault()
      switchMode(mode === 'login' ? 'register' : 'login', true)
      return
    }
    if (event.key === 'Home') {
      event.preventDefault()
      switchMode('login', true)
      return
    }
    if (event.key === 'End') {
      event.preventDefault()
      switchMode('register', true)
    }
  }

  return (
    <AuthLayout asideMode={mode}>
      <div className="uapp-seg auth-mode-seg" role="tablist" aria-label="登录与注册">
        <button
          ref={loginTabRef}
          id="auth-tab-login"
          type="button"
          role="tab"
          tabIndex={mode === 'login' ? 0 : -1}
          aria-selected={mode === 'login'}
          aria-controls="auth-panel-login"
          className={mode === 'login' ? 'uapp-seg-item active' : 'uapp-seg-item'}
          onClick={() => switchMode('login')}
          onKeyDown={handleTabKeyDown}
        >
          登录
        </button>
        <button
          ref={registerTabRef}
          id="auth-tab-register"
          type="button"
          role="tab"
          tabIndex={mode === 'register' ? 0 : -1}
          aria-selected={mode === 'register'}
          aria-controls="auth-panel-register"
          className={mode === 'register' ? 'uapp-seg-item active' : 'uapp-seg-item'}
          onClick={() => switchMode('register')}
          onKeyDown={handleTabKeyDown}
        >
          注册
        </button>
      </div>

      {/* 两签常驻堆叠，切换淡入淡出（同位溶解，无并排破碎态）；
          视口高度跟随活跃签平滑长缩，与左栏文案同步。 */}
      <div className="auth-mode-viewport" style={{ height: viewportH }}>
        <div
          className="auth-mode-pane"
          data-active={mode === 'login'}
          ref={loginPaneRef}
          id="auth-panel-login"
          role="tabpanel"
          aria-labelledby="auth-tab-login"
          aria-hidden={mode !== 'login'}
        >
          <LoginForm
            redirect={redirect}
            expired={expired}
            onSignInStart={() => setSigningIn(true)}
            onSignInFailed={() => setSigningIn(false)}
          />
        </div>
        <div
          className="auth-mode-pane"
          data-active={mode === 'register'}
          ref={registerPaneRef}
          id="auth-panel-register"
          role="tabpanel"
          aria-labelledby="auth-tab-register"
          aria-hidden={mode !== 'register'}
        >
          <RegisterForm
            status={registration.data}
            statusLoading={registration.isPending}
            statusError={registration.isError}
            onRetryStatus={() => {
              void registration.refetch()
            }}
            onSwitchToLogin={() => switchMode('login')}
            onRegisterStart={() => setSigningIn(true)}
            onRegisterFailed={() => setSigningIn(false)}
            onRegistered={(me) => {
              void navigate({
                href: landingForRole(me?.role ?? 'user'),
                replace: true,
              })
            }}
          />
        </div>
      </div>
    </AuthLayout>
  )
}
