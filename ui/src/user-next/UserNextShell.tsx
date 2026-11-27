/**
 * **功能**
 * user-next 应用壳：会话守卫、纸感顶栏、三签导航与移动底栏。
 *
 * **归属**
 * user-next 隔离层：只负责壳与导航，页面内容走 <Outlet>。
 *
 * **口径**
 * 三签是今天 / 记录 / 我的；账号与设置收进「我的」。
 * 旧路径由 router 重定向，壳内不保留第二套导航。
 * 顶栏与底栏都不使用玻璃、发光或胶囊堆叠；状态只靠字重、细线与雾蓝。
 */

import { useEffect, type MouseEvent } from 'react'
import { Link, Navigate, Outlet, useNavigate, useRouterState } from '@tanstack/react-router'
import { Icon } from '@/components/ui/Icon'
import { cx } from '@/lib/cx'
import { useMe, useSessionExpired } from '@/lib/session'
import { originFromEvent, useTheme } from '@/lib/theme'
import { withViewTransition } from './lib/viewTransition'

type NavItem = {
  to: '/app' | '/app/history' | '/app/me'
  label: string
  title: string
}

const NAV: readonly NavItem[] = [
  { to: '/app', label: '今天', title: '今天' },
  { to: '/app/history', label: '记录', title: '记录' },
  { to: '/app/me', label: '我的', title: '我的' },
]

const TITLES: Record<string, string> = {
  '/app': '今天',
  '/app/history': '记录',
  '/app/me': '我的',
  '/app/me/account': '签到账号',
  '/app/me/account/new': '添加账号',
  '/app/me/time': '签到时间',
  '/app/me/password': '修改密码',
}

function titleFor(pathname: string): string {
  if (TITLES[pathname]) return TITLES[pathname]
  if (pathname.startsWith('/app/history')) return '记录'
  if (pathname.startsWith('/app/me')) return '我的'
  return '签到助手'
}

function plainLeftClick(event: MouseEvent<HTMLElement>): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey
}

function navActive(item: NavItem, pathname: string): boolean {
  if (item.to === '/app') return pathname === '/app'
  if (item.to === '/app/history') return pathname === '/app/history'
  return pathname === '/app/me' || pathname.startsWith('/app/me/')
}

function ShellLoading() {
  return (
    <div
      className="un-root uapp un-shell-loading"
      role="status"
      aria-busy="true"
      aria-label="正在加载签到助手"
    >
      <div className="un-container un-shell-loading__inner">
        <span className="un-sr-only">正在加载签到助手。</span>
        <span className="un-skeleton un-shell-loading__bar" />
        <span className="un-skeleton un-shell-loading__title" />
        <span className="un-skeleton un-shell-loading__sheet" />
      </div>
    </div>
  )
}

export function UserNextShell() {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const searchStr = useRouterState({ select: (state) => state.location.searchStr })
  const navigate = useNavigate()
  const meQuery = useMe()
  const expired = useSessionExpired()
  const { theme, toggleTheme } = useTheme()

  useEffect(() => {
    document.title = `${titleFor(pathname)} · 签到助手`
  }, [pathname])

  if (meQuery.isPending) return <ShellLoading />
  if (!meQuery.data) {
    if (pathname === '/login') return null
    return (
      <Navigate
        to="/login"
        search={expired ? {} : { redirect: `${pathname}${searchStr}` }}
        replace
      />
    )
  }

  const me = meQuery.data
  if (me.role !== 'user') return <Navigate to="/" replace />

  const nextTheme = theme === 'dark' ? '浅色' : '深色'
  const pageTitle = titleFor(pathname)

  return (
    <div className="un-root uapp">
      <a className="un-skip uapp-skip" href="#uapp-main">
        跳到正文
      </a>

      <header className="un-bar uapp-bar">
        <div className="un-container un-bar__inner">
          <div className="un-bar__brand-group">
            <Link to="/app" className="un-brand uapp-brand" aria-label="签到助手 · 今天">
              <Icon name="scrollText" size={20} className="un-brand__glyph" />
              <span className="un-brand__text">签到助手</span>
            </Link>
            <span className="un-bar__page-title" aria-hidden="true">
              {pageTitle}
            </span>
          </div>

          <nav className="un-nav uapp-top-nav" aria-label="主导航">
            {NAV.map((item) => {
              const active = navActive(item, pathname)
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  className={cx('un-nav__link', 'uapp-top-link', active && 'active')}
                  activeOptions={{ exact: item.to !== '/app/me' }}
                  aria-current={active ? 'page' : undefined}
                  onClick={(event) => {
                    if (active || !plainLeftClick(event)) return
                    event.preventDefault()
                    withViewTransition('page', () => navigate({ to: item.to }))
                  }}
                >
                  {item.label}
                </Link>
              )
            })}
          </nav>

          <div className="un-bar__actions uapp-bar-actions">
            <button
              type="button"
              className="un-icon-btn uapp-icon-btn"
              aria-label={`切换到${nextTheme}主题`}
              title={`切换到${nextTheme}主题`}
              onClick={(event) => toggleTheme(originFromEvent(event))}
            >
              <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={18} />
            </button>

            <Link
              to="/app/me"
              className="un-avatar-link"
              aria-label="打开我的档案"
              onClick={(event) => {
                if (pathname === '/app/me' || !plainLeftClick(event)) return
                event.preventDefault()
                withViewTransition('identity', () => navigate({ to: '/app/me' }))
              }}
            >
              <span className="un-avatar un-avatar--sm" aria-hidden="true">
                我
              </span>
            </Link>
          </div>
        </div>
      </header>

      <main className="un-main uapp-main" id="uapp-main" tabIndex={-1}>
        <div className="un-container">
          {me.must_change_password ? (
            <div className="un-notice" role="status">
              <span>登录口令需要重置。</span>
              <Link to="/app/me/password" className="un-link">
                去修改
              </Link>
            </div>
          ) : null}

          <Outlet />
        </div>
      </main>

      <nav className="un-bottom uapp-bottom" aria-label="主导航">
        {NAV.map((item) => {
          const active = navActive(item, pathname)
          return (
            <Link
              key={item.to}
              to={item.to}
              className={cx('un-bottom__item', 'uapp-tab', active && 'active')}
              activeOptions={{ exact: item.to !== '/app/me' }}
              aria-current={active ? 'page' : undefined}
              onClick={(event) => {
                if (active || !plainLeftClick(event)) return
                event.preventDefault()
                withViewTransition('page', () => navigate({ to: item.to }))
              }}
            >
              <span className="un-bottom__dot" aria-hidden="true" />
              <span className="un-bottom__label">{item.label}</span>
            </Link>
          )
        })}
      </nav>
    </div>
  )
}
