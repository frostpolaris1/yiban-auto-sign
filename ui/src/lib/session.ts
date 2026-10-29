/**
 * **功能**
 * 会话查询与「会话已过期」标记。
 *
 * **归属**
 * `lib` 工具层：全仓唯一的 `['me']` 查询定义与过期标记。
 *
 * **口径**
 * `GET /api/auth/me` 的 401 是未登录常态，故 `retry: false`。
 * 标记是进程内状态，不进 localStorage；刷新页面即复位。
 * 过期标记由业务端点 401 触发，或由改密的 me 探针在 me 也 401 时触发。
 * 登录接口与 me 探针自身的 401 不触发。
 */

import { useSyncExternalStore } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { fetchMe, type SessionUser } from './api/auth'
import { setCsrfToken } from './api/client'
import { queryClient } from './query'
import { SHOWCASE } from './showcase-mode'

export const ME_QUERY_KEY = ['me'] as const

/**
 * 展示模式（VITE_SHOWCASE=1 构建）：不发会话请求，返回合成 master 会话。
 * 用于纯静态展示部署（如 probe 机）：访客可浏览全部页面与组件画廊，
 * 各页数据取展示夹具（showcase.ts），无任何网络请求。
 * 写入口由各 feature 按 `READ_ONLY_REASON` 置灰。
 * 判定唯一实现在 `showcase-mode.ts`，此处转出给历史读者。
 */
export { SHOWCASE }

const SHOWCASE_USER: SessionUser = {
  id: 'showcase',
  email: 'showcase@demo.local',
  role: 'master',
  status: 'active',
  must_change_password: false,
  csrf_token: 'showcase',
}

/** 守卫、登录页、账号页共用这一份查询。 */
export function useMe(): UseQueryResult<SessionUser> {
  return useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: async (): Promise<SessionUser> => {
      if (SHOWCASE) return SHOWCASE_USER
      return fetchMe()
    },
    retry: false,
    staleTime: SHOWCASE ? Infinity : undefined,
  })
}

let sessionExpired = false
const listeners = new Set<() => void>()

function emit(): void {
  for (const listener of listeners) listener()
}

export function isSessionExpired(): boolean {
  return sessionExpired
}

export function markSessionExpired(): void {
  if (sessionExpired) return
  sessionExpired = true
  emit()
}

export function clearSessionExpired(): void {
  if (!sessionExpired) return
  sessionExpired = false
  emit()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function useSessionExpired(): boolean {
  return useSyncExternalStore(subscribe, isSessionExpired, isSessionExpired)
}

/** 登录页导航的最小形状。`lib` 不认识路由实例，由调用方给出。 */
type NavigateToLogin = (options: { to: '/login'; replace: true }) => unknown

/**
 * 会话失效的统一处置。
 * 标记过期、作废 CSRF、清全部缓存、落登录页。
 * 会话没了，旧令牌与旧缓存都不该被下一次登录或守卫读到。
 * 业务端点 401 的全局回调与改密探针的结论都走这里。
 */
export function expireSession(navigate: NavigateToLogin): void {
  markSessionExpired()
  setCsrfToken(null)
  queryClient.clear()
  void navigate({ to: '/login', replace: true })
}
