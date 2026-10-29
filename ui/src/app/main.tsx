/**
 * **功能**
 * 应用入口。挂载 React 根节点，注册全局 401 处置。
 * 样式表按 vendor、令牌、项目覆盖三层加载。
 *
 * **归属**
 * `app` 装配层：只组合，不承载域逻辑。
 *
 * **口径**
 * 样式顺序即层叠优先级。后表覆盖前表，不许调换。
 * 根节点 id 固定 `root`。缺失时抛错，不静默降级。
 * 业务端点 401 ⇒ 标记会话过期、清会话缓存、落登录页。
 * 登录接口、改密接口与 me 探针的 401 不走这条路（分类在 `lib/api/auth.ts`）。
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from '@tanstack/react-router'
import { AppProviders } from './providers'
import { router } from './router'
import { isSessionExpiry } from '@/lib/api/auth'
import { setCsrfToken, setUnauthorizedHandler } from '@/lib/api/client'
import { queryClient } from '@/lib/query'
import { markSessionExpired } from '@/lib/session'
import '@/styles/adminator.css'
import '@/styles/tokens.css'
import '@/styles/app.css'

setUnauthorizedHandler((path) => {
  if (!isSessionExpiry(path)) return
  markSessionExpired()
  // 会话没了，令牌也作废：别把旧 CSRF 头带进下一次登录请求。
  setCsrfToken(null)
  // 丢掉全部缓存：留着旧 me 会让守卫和登录页都以为还有会话；
  // 其余域也可能带着上一会话的数据。与登出的清理口径一致。
  queryClient.clear()
  void router.navigate({ to: '/login', replace: true })
})

const container = document.getElementById('root')
if (container === null) {
  throw new Error('#root element is missing from index.html')
}

createRoot(container).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
)
