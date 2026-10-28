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
 * 过期标记只由业务端点 401 触发，登录接口与 me 探针不触发。
 */

import { useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchMe } from './api/auth'

export const ME_QUERY_KEY = ['me'] as const

/** 守卫、登录页、账号页共用这一份查询。 */
export function useMe() {
  return useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: fetchMe,
    retry: false,
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
