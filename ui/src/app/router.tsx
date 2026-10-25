import { createRootRoute, createRoute, createRouter, Outlet } from '@tanstack/react-router'
import { AppShell } from '@/components/shell/AppShell'
import { DashboardPage } from '@/pages/DashboardPage'
import { DesignPage } from '@/pages/DesignPage'
import { LoginPage } from '@/pages/LoginPage'
import { NotFoundPage } from '@/pages/NotFoundPage'

const rootRoute = createRootRoute({
  component: () => <Outlet />,
  notFoundComponent: NotFoundPage,
})

/** Pathless layout route: every child renders inside the Adminator shell. */
const shellRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: '_shell',
  component: AppShell,
})

const dashboardRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/',
  component: DashboardPage,
})

const designRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/design',
  component: DesignPage,
})

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: LoginPage,
})

const routeTree = rootRoute.addChildren([
  shellRoute.addChildren([dashboardRoute, designRoute]),
  loginRoute,
])

export const router = createRouter({
  routeTree,
  defaultPreload: 'intent',
})

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
