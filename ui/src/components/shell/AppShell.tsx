/**
 * **功能**
 * 应用壳：会话守卫、侧栏、顶栏、内容区、页脚、抽屉遮罩。
 *
 * **归属**
 * `components/shell`：摆骨架，页面内容走 `<Outlet>`。
 *
 * **口径**
 * 抽屉开合由 `body.has-drawer-open` 表达。vendor 的 CSS 认这个类。
 * 路由变化、Esc、点遮罩都关闭抽屉。
 * 会话解析中显示整页加载态；无会话落到 `/login` 并带上站内回跳地址。
 * `must_change_password` 为真时内容区顶部常驻提示条。
 */

import { useEffect, useState } from 'react'
import { Navigate, Outlet, useNavigate, useRouterState } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { SessionLoading } from '@/components/ui/SessionLoading'
import { logout } from '@/lib/api/auth'
import { describeApiError } from '@/lib/api/client'
import { clearSessionExpired, SHOWCASE, useMe, useSessionExpired } from '@/lib/session'
import { crumbsFor, documentTitleFor } from './nav'
import { Footer } from './Footer'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'

/**
 * vendor 的 `Shell.js` 往占位符里注入 DOM。本仓改为常驻组件。
 * 遮罩是普通元素，可见性由 `body.has-drawer-open` 决定。
 * 改成条件渲染会让 vendor 的过渡失效。
 */
export function AppShell() {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const meQuery = useMe()
  const expired = useSessionExpired()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [logoutError, setLogoutError] = useState<string | null>(null)

  const logoutMutation = useMutation({
    mutationFn: async () => {
      // 展示模式：无后端，退出只是回到登录页（会话由 useMe 常驻提供）。
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
    document.body.classList.toggle('has-drawer-open', drawerOpen)
    return () => {
      document.body.classList.remove('has-drawer-open')
    }
  }, [drawerOpen])

  useEffect(() => {
    setDrawerOpen(false)
  }, [pathname])

  useEffect(() => {
    document.title = documentTitleFor(pathname)
  }, [pathname])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDrawerOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [])

  // 首次 me 未返回前只显示加载态：不先渲染登录页，也不渲染壳。
  if (meQuery.isPending) return <SessionLoading />
  if (!meQuery.data) {
    // `/login` 在壳外。壳还能渲染到它，说明路由正在切换：
    // 让新路由接管，别把 `/login` 自己写成回跳地址。
    if (pathname === '/login') return null
    return <Navigate to="/login" search={expired ? {} : { redirect: pathname }} replace />
  }

  const me = meQuery.data

  return (
    <div className="shell">
      <Sidebar me={me} pathname={pathname} onNavigate={() => setDrawerOpen(false)} />
      <div className="main">
        <Topbar
          me={me}
          crumbs={crumbsFor(pathname)}
          drawerOpen={drawerOpen}
          onOpenDrawer={() => setDrawerOpen(true)}
          onLogout={() => {
            setLogoutError(null)
            logoutMutation.mutate()
          }}
          loggingOut={logoutMutation.isPending}
        />
        <main className="content app-content">
          {logoutError ? (
            <div className="app-alert-block">
              <Alert tone="danger" title="退出失败">
                {logoutError}
              </Alert>
            </div>
          ) : null}

          {SHOWCASE ? (
            <div className="app-alert-block">
              <Alert tone="info" title="展示模式">
                纯静态演示：数据为示例值，写操作全部置灰。
              </Alert>
            </div>
          ) : null}

          {me.must_change_password ? (
            <div className="app-alert-block">
              <Alert tone="warning" title="需要重置密码">
                账号需要重置密码，请联系管理员
              </Alert>
            </div>
          ) : null}

          <Outlet />
        </main>
        <Footer />
      </div>
      <div
        className="drawer-backdrop"
        role="presentation"
        aria-hidden="true"
        onClick={() => setDrawerOpen(false)}
      />
    </div>
  )
}
