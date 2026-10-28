/**
 * **功能**
 * 路由表：根路由、壳布局、五条页面路由。
 *
 * **归属**
 * `app` 装配层：只做装配，页面逻辑留在 `pages`。
 *
 * **口径**
 * 路径是侧栏与面包屑的共同坐标。
 * 新增页面必须同时改本文件与 `nav.ts`，否则导航缺项。
 */

import { createRootRoute, createRoute, createRouter, Outlet } from '@tanstack/react-router'
import { AppShell } from '@/components/shell/AppShell'
import { AccountsPage } from '@/pages/AccountsPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { DesignPage } from '@/pages/DesignPage'
import { LoginPage } from '@/pages/LoginPage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { SchoolsPage } from '@/pages/SchoolsPage'
import { sanitizeRedirect } from '@/lib/redirect'

const rootRoute = createRootRoute({
  component: () => <Outlet />,
  notFoundComponent: NotFoundPage,
})

/**
 * **无路径**布局路由：URL 不出现前缀，子路由共用 Adminator 壳。
 * 改成带路径会让 URL 多一层前缀，面包屑与深链都要跟着改。
 */
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

const accountsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/accounts',
  component: AccountsPage,
})

const schoolsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/schools',
  component: SchoolsPage,
})

/**
 * 登录路由的自定义 search：`?redirect=`。
 * 校验即净化：站外地址在解析时丢弃，后续读者拿不到它。
 */
const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  validateSearch: (search: Record<string, unknown>): { redirect?: string } => {
    const redirect = sanitizeRedirect(search.redirect)
    return redirect ? { redirect } : {}
  },
  component: LoginRouteComponent,
})

/** 把类型化的 search 传给页面。页面不直接依赖路由定义。 */
function LoginRouteComponent() {
  const { redirect } = loginRoute.useSearch()
  return <LoginPage redirect={redirect} />
}

const routeTree = rootRoute.addChildren([
  shellRoute.addChildren([dashboardRoute, designRoute, accountsRoute, schoolsRoute]),
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
