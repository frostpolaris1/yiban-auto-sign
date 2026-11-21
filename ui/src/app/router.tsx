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

import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  redirect,
  useNavigate,
  useRouterState,
} from '@tanstack/react-router'
import { AppShell } from '@/components/shell/AppShell'
import { UserAppShell } from '@/components/user-app/UserAppShell'
import { AccountsPage } from '@/pages/AccountsPage'
import { AuditPage } from '@/pages/AuditPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { DesignPage } from '@/pages/DesignPage'
import { ForgotPasswordPage } from '@/pages/ForgotPasswordPage'
import { LoginPage } from '@/pages/LoginPage'
import { LogsPage } from '@/pages/LogsPage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { QueuePage } from '@/pages/QueuePage'
import { ResetPasswordPage } from '@/pages/ResetPasswordPage'
import { SchoolsPage } from '@/pages/SchoolsPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { SystemPage } from '@/pages/SystemPage'
import { UsersPage } from '@/pages/UsersPage'
import { UserAccountNewPage } from '@/pages/user-app/UserAccountNewPage'
import { UserAccountPage } from '@/pages/user-app/UserAccountPage'
import { UserHistoryPage } from '@/pages/user-app/UserHistoryPage'
import { UserHomePage } from '@/pages/user-app/UserHomePage'
import { UserPasswordPage } from '@/pages/user-app/UserPasswordPage'
import { UserSettingsPage } from '@/pages/user-app/UserSettingsPage'
import { UserTimePrefPage } from '@/pages/user-app/UserTimePrefPage'
import { parseRunSearchRecord, runSearchRecord } from '@/features/logs/urlState'
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

/**
 * 用户端独立无路径布局：URL 前缀 `/app`，不复用 Adminator 壳。
 * 三个一级页：主页 / 历史 / 设置；账号详情是二级页。
 */
const userAppRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: '_userApp',
  component: UserAppShell,
})

const userHomeRoute = createRoute({
  getParentRoute: () => userAppRoute,
  path: '/app',
  component: UserHomePage,
})

const userHistoryRoute = createRoute({
  getParentRoute: () => userAppRoute,
  path: '/app/history',
  validateSearch: (search: Record<string, unknown>): { day?: string } => {
    const day = parseDay(search.day)
    return day ? { day } : {}
  },
  component: UserHistoryRouteComponent,
})

function UserHistoryRouteComponent() {
  const { day } = userHistoryRoute.useSearch()
  const navigate = useNavigate()
  return (
    <UserHistoryPage
      day={day}
      onDayChange={(next) => {
        void navigate({ to: '/app/history', search: { day: next }, replace: true })
      }}
    />
  )
}

const userSettingsRoute = createRoute({
  getParentRoute: () => userAppRoute,
  path: '/app/settings',
  component: UserSettingsPage,
})

const userAccountRoute = createRoute({
  getParentRoute: () => userAppRoute,
  path: '/app/account',
  component: UserAccountPage,
})

const userAccountNewRoute = createRoute({
  getParentRoute: () => userAppRoute,
  path: '/app/account/new',
  component: UserAccountNewPage,
})

const userTimePrefRoute = createRoute({
  getParentRoute: () => userAppRoute,
  path: '/app/settings/time-pref',
  component: UserTimePrefPage,
})

const userPasswordRoute = createRoute({
  getParentRoute: () => userAppRoute,
  path: '/app/settings/password',
  component: UserPasswordPage,
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

/** 账号设置（个人域）：账号信息、改密与本人通知订阅。master/admin 进页，user 渲染无权限卡片。 */
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

/** 旧运行页入口：保底一个版本周期，302 到签到台账·签到事件页签并保留合法筛选。 */
const runRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/run',
  validateSearch: (search: Record<string, unknown>) => parseRunSearchRecord(search),
  beforeLoad: ({ search }) => {
    throw redirect({
      to: '/logs',
      search: runSearchRecord({ ...search, tab: 'events' }),
      replace: true,
    })
  },
})

/** 旧执行监控入口：保底一个版本周期，302 到签到台账·运行巡检页签。 */
const engineRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/engine',
  beforeLoad: () => {
    throw redirect({
      to: '/logs',
      search: { tab: 'patrol' },
      replace: true,
    })
  },
})

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
 * 待办工作台：master/admin。权限门由 QueueView 内部执行。
 */
const queueRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/queue',
  component: QueuePage,
})

/**
 * 签到台账：master/admin。合并签到事件与运行巡检两页签；
 * 页签、筛选、业务日与游标均为 URL 状态（校验即净化）。
 */
const logsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/logs',
  validateSearch: (search: Record<string, unknown>) => parseRunSearchRecord(search),
  component: LogsRouteComponent,
})

/** 把类型化的 search 传给签到台账页，并让页面通过回调写回查询串。 */
function LogsRouteComponent() {
  const search = logsRoute.useSearch()
  const navigate = useNavigate()
  return (
    <LogsPage
      search={parseRunSearchRecord(search as Record<string, unknown>)}
      onSearchChange={(next, opts) => {
        void navigate({ to: '/logs', search: runSearchRecord(next), replace: opts?.replace })
      }}
    />
  )
}

/** 审计日志：master/admin。权限门由 AuditView 内部执行，user 不发审计请求。 */
const auditRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/audit',
  component: AuditPage,
})

/**
 * 系统设置：master/admin。权限门由 SystemView 内部执行，user 不发业务请求。
 * `?day=YYYY-MM-DD` 校验即净化：非法日期在解析时丢弃，计划分区回落到今天。
 */
const systemRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/system',
  validateSearch: (search: Record<string, unknown>): { day?: string } => ({
    day: parseDay(search.day) ?? undefined,
  }),
  component: SystemRouteComponent,
})

/** 把类型化的 search 传给系统设置页，并让计划分区通过回调写回查询串。 */
function SystemRouteComponent() {
  const { day } = systemRoute.useSearch()
  const navigate = useNavigate()
  return (
    <SystemPage
      day={parseDay(day) ?? undefined}
      onDayChange={(next) => {
        void navigate({ to: '/system', search: { day: next }, replace: true })
      }}
    />
  )
}

/** 旧时间片入口：保底一个版本周期，302 到系统设置·签到计划分区（保留合法 ?day= 并锚定 #plans）。 */
const scheduleRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/schedule',
  validateSearch: (search: Record<string, unknown>): { day?: string } => ({
    day: parseDay(search.day) ?? undefined,
  }),
  beforeLoad: ({ search }) => {
    const day = parseDay(search.day) ?? undefined
    throw redirect({
      to: '/system',
      search: () => (day ? { day } : {}),
      hash: 'plans',
      replace: true,
    })
  },
})

/**
 * 认证双签常驻布局。
 * 登录/注册是同页两签，切换走双向交叉横滑——LoginPage 必须跨切换常驻，
 * 不能随路由重挂载。/login 与 /register 只做路径匹配标记，模式由 pathname 推导。
 */
const authModeRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: '_authMode',
  component: AuthModeLayout,
})

function AuthModeLayout() {
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const search = useRouterState({ select: (s) => s.location.search }) as { redirect?: string }
  return (
    <>
      <LoginPage
        mode={pathname === '/register' ? 'register' : 'login'}
        redirect={sanitizeRedirect(search.redirect) ?? undefined}
      />
      <Outlet />
    </>
  )
}

const loginRoute = createRoute({
  getParentRoute: () => authModeRoute,
  path: '/login',
  validateSearch: (search: Record<string, unknown>): { redirect?: string } => {
    const redirect = sanitizeRedirect(search.redirect)
    return redirect ? { redirect } : {}
  },
  component: () => null,
})

/** 注册是登录页的第二签；`/register` 只做深链别名。 */
const registerRoute = createRoute({
  getParentRoute: () => authModeRoute,
  path: '/register',
  validateSearch: (search: Record<string, unknown>): { redirect?: string } => {
    const redirect = sanitizeRedirect(search.redirect)
    return redirect ? { redirect } : {}
  },
  component: () => null,
})

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
    queueRoute,
    logsRoute,
    auditRoute,
    systemRoute,
    scheduleRoute,
  ]),
  userAppRoute.addChildren([
    userHomeRoute,
    userHistoryRoute,
    userSettingsRoute,
    userAccountRoute,
    userAccountNewRoute,
    userTimePrefRoute,
    userPasswordRoute,
  ]),
  authModeRoute.addChildren([loginRoute, registerRoute]),
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
