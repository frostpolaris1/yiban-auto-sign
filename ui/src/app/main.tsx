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
 * 业务端点 401 ⇒ 统一会话过期处置（`lib/session` 的 `expireSession`）。
 * 登录、改密与 me 探针的 401 不走全局这条路；改密由 me 探针定论。
 * 分类规则在 `lib/api/auth.ts`。
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from '@tanstack/react-router'
import { AppProviders } from './providers'
import { router } from './router'
import { isSessionExpiry } from '@/lib/api/auth'
import { setUnauthorizedHandler } from '@/lib/api/client'
import { expireSession } from '@/lib/session'
import '@/styles/adminator.css'
import '@/styles/tokens.css'
import '@/styles/app.css'

setUnauthorizedHandler((path) => {
  if (!isSessionExpiry(path)) return
  expireSession((options) => router.navigate(options))
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
