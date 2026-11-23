/**
 * **功能**
 * 用户端应用壳：会话守卫、顶栏、底部标签、内容区。
 *
 * **归属**
 * `components/user-app`：只摆壳，页面内容走 `<Outlet>`。
 *
 * **口径**
 * 只服务 `role=user`；admin/master 直接访问 `/app` 回管理台。
 * 移动 <768：顶栏 + 底栏；≥768：顶栏横向导航，无侧栏、无面包屑。
 * 会话解析中整页 loading；无会话落 `/login` 并带回跳地址。
 * 页面标题由路径派生；不做页面左右滑入。
 * 头像菜单只放账号设置与退出，不复制管理台菜单。
 */

import { useEffect, useState } from 'react'
import { Link, Navigate, Outlet, useNavigate, useRouterState } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { BrandMark } from '@/components/ui/BrandMark'
import { Icon, type IconName } from '@/components/ui/Icon'
import { Menu, MenuItem } from '@/components/ui/Menu'
import { SessionLoading } from '@/components/ui/SessionLoading'
import { logout } from '@/lib/api/auth'
import { describeApiError } from '@/lib/api/client'
import { cx } from '@/lib/cx'
import { clearSessionExpired, SHOWCASE, useMe, useSessionExpired } from '@/lib/session'
import { originFromEvent, useTheme } from '@/lib/theme'

type UserNavItem = {
  to: '/app' | '/app/history' | '/app/settings'
  label: string
  icon: IconName
  title: string
}

export const USER_NAV: readonly UserNavItem[] = [
  { to: '/app', label: '主页', icon: 'home', title: '主页' },
  { to: '/app/history', label: '历史', icon: 'history', title: '历史' },
  { to: '/app/settings', label: '设置', icon: 'settings', title: '设置' },
]

const TITLES: Record<string, string> = {
  '/app': '主页',
  '/app/history': '历史',
  '/app/settings': '设置',
  '/app/account': '账号',
  '/app/account/new': '新建账号',
  '/app/settings/time-pref': '签到时间',
  '/app/settings/password': '修改密码',
}

function titleFor(pathname: string): string {
  if (TITLES[pathname]) return TITLES[pathname]
  const leaf = USER_NAV.find((entry) => pathname.startsWith(`${entry.to}/`))
  return leaf ? leaf.title : '签到助手'
}

export function UserAppShell() {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const searchStr = useRouterState({ select: (state) => state.location.searchStr })
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const meQuery = useMe()
  const expired = useSessionExpired()
  const { theme, toggleTheme } = useTheme()
  const [logoutError, setLogoutError] = useState<string | null>(null)

  const logoutMutation = useMutation({
    mutationFn: async () => {
      if (SHOWCASE) return
      await logout()
    },
    onSuccess: () => {
      clearSessionExpired()
      if (!SHOWCASE) queryClient.clear()
      void navigate({ to: '/login', replace: true })
    },
    onError: (error) => setLogoutError(describeApiError(error)),
  })

  useEffect(() => {
    document.title = `${titleFor(pathname)} · 签到助手`
  }, [pathname])

  if (meQuery.isPending) return <SessionLoading />
  if (!meQuery.data) {
    if (pathname === '/login') return null
    // 回跳带上查询串，筛选/选中日不丢。
    return (
      <Navigate
        to="/login"
        search={expired ? {} : { redirect: `${pathname}${searchStr}` }}
        replace
      />
    )
  }

  const me = meQuery.data
  // 用户端只归 user；管理员走管理台，不在这里给第二套导航。
  if (me.role !== 'user') return <Navigate to="/" replace />

  const nextTheme = theme === 'dark' ? '浅色' : '深色'
  // 账号详情/新建/照片是二级页：底栏与顶栏都继续高亮「主页」。
  const homeActive = pathname === '/app' || pathname.startsWith('/app/account')

  return (
    <div className="uapp">
      <a className="uapp-skip" href="#uapp-main">
        跳转到正文
      </a>
      <header className="uapp-bar">
        <div className="uapp-bar-inner">
          <Link to="/app" className="uapp-brand" aria-label="签到助手主页">
            <span className="uapp-brand-mark">
              <BrandMark />
            </span>
            <span className="uapp-brand-text">签到助手</span>
          </Link>

          <nav className="uapp-top-nav" aria-label="主导航">
            {USER_NAV.map((item) => (
              <Link
                key={item.to}
                to={item.to}
                className={cx(
                  'uapp-top-link',
                  item.to === '/app' && homeActive && 'active',
                )}
                activeOptions={{ exact: item.to === '/app' }}
                aria-current={item.to === '/app' && homeActive ? 'page' : undefined}
              >
                {item.label}
              </Link>
            ))}
          </nav>

          <div className="uapp-bar-actions">
            <button
              type="button"
              className="uapp-icon-btn"
              aria-label={`切换到${nextTheme}主题`}
              title={`切换到${nextTheme}主题`}
              onClick={(event) => toggleTheme(originFromEvent(event))}
            >
              <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={18} />
            </button>

            <Menu
              profile
              trigger={
                <button type="button" className="uapp-avatar" aria-label="账号菜单">
                  <Icon name="user" size={16} />
                </button>
              }
            >
              <MenuItem onSelect={() => void navigate({ to: '/app/settings' })}>
                <Icon name="settings" size={15} />
                账号设置
              </MenuItem>
              <MenuItem danger onSelect={() => logoutMutation.mutate()}>
                <Icon name="logout" size={15} />
                退出登录
              </MenuItem>
            </Menu>
          </div>
        </div>
      </header>

      <main className="uapp-main" id="uapp-main" tabIndex={-1}>
        <div className="uapp-content">
          {logoutError ? (
            <div className="uapp-alert-block">
              <Alert tone="danger" title="退出失败">
                {logoutError}
              </Alert>
            </div>
          ) : null}

          {me.must_change_password ? (
            <div className="uapp-notice" role="status">
              <span>登录口令需要重置。</span>
              <Link to="/app/settings">去修改</Link>
            </div>
          ) : null}

          <Outlet />
        </div>
      </main>

      <nav className="uapp-bottom" aria-label="主导航">
        {USER_NAV.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            className={cx('uapp-tab', item.to === '/app' && homeActive && 'active')}
            activeOptions={{ exact: item.to === '/app' }}
            aria-current={item.to === '/app' && homeActive ? 'page' : undefined}
          >
            <span className="uapp-tab-icon">
              <Icon name={item.icon} size={20} />
            </span>
            <span className="uapp-tab-label">{item.label}</span>
          </Link>
        ))}
      </nav>
    </div>
  )
}
