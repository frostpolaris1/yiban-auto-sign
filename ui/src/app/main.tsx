/**
 * **功能**
 * 应用入口。挂载 React 根节点。
 * 样式表按 vendor、令牌、项目覆盖三层加载。
 *
 * **归属**
 * `app` 装配层：只组合，不承载域逻辑。
 *
 * **口径**
 * 样式顺序即层叠优先级。后表覆盖前表，不许调换。
 * 根节点 id 固定 `root`。缺失时抛错，不静默降级。
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from '@tanstack/react-router'
import { AppProviders } from './providers'
import { router } from './router'
import '@/styles/adminator.css'
import '@/styles/tokens.css'
import '@/styles/app.css'

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
