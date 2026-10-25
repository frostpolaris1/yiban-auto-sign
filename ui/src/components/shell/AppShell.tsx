/**
 * **功能**
 * 应用壳：侧栏、顶栏、内容区、页脚、抽屉遮罩。
 *
 * **归属**
 * `components/shell`：只摆骨架，页面内容走 `<Outlet>`。
 *
 * **口径**
 * 抽屉开合由 `body.has-drawer-open` 表达。vendor 的 CSS 认这个类。
 * 路由变化、Esc、点遮罩都关闭抽屉。
 */

import { useEffect, useState } from 'react'
import { Outlet, useRouterState } from '@tanstack/react-router'
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
  const [drawerOpen, setDrawerOpen] = useState(false)

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

  return (
    <div className="shell">
      <Sidebar pathname={pathname} onNavigate={() => setDrawerOpen(false)} />
      <div className="main">
        <Topbar
          crumbs={crumbsFor(pathname)}
          drawerOpen={drawerOpen}
          onOpenDrawer={() => setDrawerOpen(true)}
        />
        <main className="content app-content">
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
