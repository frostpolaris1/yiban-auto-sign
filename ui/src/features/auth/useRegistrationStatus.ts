/**
 * **功能**
 * 注册状态查询：开关与协议版本。
 *
 * **归属**
 * `features/auth` 域钩子：登录/注册页与法务页共用一份缓存。
 *
 * **口径**
 * 公开端点，未登录也能读；展示构建返回固定开放的合成状态。
 * 失败不阻断登录页；注册面板与法务页各自展示错误并给重试。
 * 协议版本是请求体的必填字段，读不到就不允许提交。
 */

import { useQuery } from '@tanstack/react-query'
import { getRegistrationStatus } from '@/lib/api/auth'
import { SHOWCASE } from '@/lib/session'
import { showcaseRegistrationStatus } from '@/lib/showcase'

export const REGISTRATION_QUERY_KEY = ['auth-registration'] as const

export function useRegistrationStatus() {
  return useQuery({
    queryKey: REGISTRATION_QUERY_KEY,
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseRegistrationStatus()) : getRegistrationStatus(),
    retry: false,
    staleTime: 60_000,
  })
}
