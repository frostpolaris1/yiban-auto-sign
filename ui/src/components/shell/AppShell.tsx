import { useEffect, useState } from 'react'
import { Outlet, useRouterState } from '@tanstack/react-router'
import { crumbsFor, documentTitleFor } from './nav'
import { Footer } from './Footer'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'

/**
 * Adminator app shell.
 *
 * The vendor `Shell.js` injected the chrome into placeholders and injected a
 * drawer backdrop on demand. React owns both: the three parts are components,
 * and the backdrop is a normal element whose visibility comes from
 * `body.has-drawer-open`, which Adminator's CSS already understands.
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
