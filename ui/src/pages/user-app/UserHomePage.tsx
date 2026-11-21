/**
 * **功能**
 * 用户端主页路由页：只装配 `features/me` 的首页容器。
 *
 * **归属**
 * `pages/user-app` 路由页：不承载取数与展示逻辑。
 *
 * **口径**
 * `/app` 是 user 登录后的默认落地；数据与状态矩阵都在 `HomeView`。
 */

import { HomeView } from '@/features/me/HomeView'

export function UserHomePage() {
  return <HomeView />
}
