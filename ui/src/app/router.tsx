/**
 * **功能**
 * 路由表：根路由、壳布局、壳内页面与公开认证页。
 *
 * **归属**
 * `app` 装配层：只做装配，页面逻辑留在 `pages`。
 *
 * **口径**
 * 路径是侧栏与面包屑的共同坐标。
 * 新增壳内页面必须同时改本文件与 `nav.ts`，否则导航缺项。
 * 公开认证页（登录、忘记密码、重置密码）挂在根路由下：不套壳。
 */

import { createRootRoute, createRoute, createRouter, Outlet, useNavigate } from '@tanstack/react-router'
import { AppShell } from '@/components/shell/AppShell'
import { AccountsPage } from '@/pages/AccountsPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { DesignPage } from '@/pages/DesignPage'
import { EnginePage } from '@/pages/EnginePage'
import { ForgotPasswordPage } from '@/pages/ForgotPasswordPage'
import { LoginPage } from '@/pages/LoginPage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { ResetPasswordPage } from '@/pages/ResetPasswordPage'
import { RunPage } from '@/pages/RunPage'
import { SchedulePage } from '@/pages/SchedulePage'
import { SchoolsPage } from '@/pages/SchoolsPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { UsersPage } from '@/pages/UsersPage'
import { parseRunSearchRecord, runSearchRecord } from '@/features/run/urlState'
import { parseUserSearchRecord, userSearchRecord } from '@/features/user/urlState'
import { parseDay } from '@/lib/format'
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

/** 账号设置：账号信息与自助改密。三种角色都能进。 */
const settingsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/settings',
  component: SettingsPage,
})

const schoolsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/schools',
  component: SchoolsPage,
})

/** 运行页：master/admin。筛选与业务日是 URL 状态：校验即净化。 */
const runRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/run',
  validateSearch: (search: Record<string, unknown>) => parseRunSearchRecord(search),
  component: RunRouteComponent,
})

/** 执行监控：master/admin。快照无筛选与分页，无查询串。 */
const engineRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/engine',
  component: EnginePage,
})

/** 把类型化的 search 传给页面，并让页面通过回调写回查询串。 */
function RunRouteComponent() {
  const search = runRoute.useSearch()
  const navigate = useNavigate()
  return (
    <RunPage
      search={search}
      onSearchChange={(next, opts) => {
        void navigate({ to: '/run', search: runSearchRecord(next), replace: opts?.replace })
      }}
    />
  )
}

/** 用户管理：只归 master；非 master 直接访问落到无权限卡片。
 * 筛选、含已删除与游标是 URL 状态：校验即净化，非法值在解析时丢弃。
 */
const usersRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/users',
  validateSearch: (search: Record<string, unknown>) => parseUserSearchRecord(search),
  component: UsersRouteComponent,
})

/** 把类型化的 search 传给页面，并让页面通过回调写回查询串。 */
function UsersRouteComponent() {
  const search = usersRoute.useSearch()
  const navigate = useNavigate()
  return (
    <UsersPage
      // loose search 会把手写 URL 的组合（?group=bogus、group×deleted）
      // 原样带进来：传给页面前再归一一次，非法值落到缺省。
      // 归一必须幂等：校验后的 deleted 是布尔，解析要兼容。
      search={parseUserSearchRecord(search as Record<string, unknown>)}
      onSearchChange={(next, opts) => {
        void navigate({ to: '/users', search: userSearchRecord(next), replace: opts?.replace })
      }}
    />
  )
}

/**
 * 时间片路由的自定义 search：`?day=YYYY-MM-DD`。
 * 校验即净化：非法日期在解析时丢弃，容器回落到今天。
 */
const scheduleRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/schedule',
  validateSearch: (search: Record<string, unknown>): { day?: string } => {
    const day = parseDay(search.day)
    return day ? { day } : {}
  },
  component: ScheduleRouteComponent,
})

/** 把类型化的 search 传给页面，并让页面通过回调写回查询串。 */
function ScheduleRouteComponent() {
  const { day } = scheduleRoute.useSearch()
  const navigate = useNavigate()
  return (
    <SchedulePage
      day={day}
      onDayChange={(next) => {
        void navigate({ to: '/schedule', search: { day: next }, replace: true })
      }}
    />
  )
}

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

/**
 * 忘记密码路由。公开页，不套壳。
 * 无查询串：邮箱只存在于表单状态，不进 URL。
 */
const forgotPasswordRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/forgot-password',
  component: ForgotPasswordPage,
})

/**
 * 重置密码路由的自定义 search：`?token=`。
 * 只做类型收窄，不判形状：形状判据与它的界面后果放在页面一处。
 */
const resetPasswordRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reset-password',
  validateSearch: (search: Record<string, unknown>): { token?: string } => {
    return typeof search.token === 'string' ? { token: search.token } : {}
  },
  component: ResetPasswordRouteComponent,
})

function ResetPasswordRouteComponent() {
  const { token } = resetPasswordRoute.useSearch()
  return <ResetPasswordPage token={token} />
}

const routeTree = rootRoute.addChildren([
  shellRoute.addChildren([
    dashboardRoute,
    designRoute,
    accountsRoute,
    settingsRoute,
    schoolsRoute,
    usersRoute,
    runRoute,
    engineRoute,
    scheduleRoute,
  ]),
  loginRoute,
  forgotPasswordRoute,
  resetPasswordRoute,
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
