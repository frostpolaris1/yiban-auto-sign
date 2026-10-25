import type { ReactNode } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '@/lib/theme'
import { queryClient } from '@/lib/query'

/**
 * **功能**
 * 全局 Provider 组合：主题上下文包住查询客户端。
 *
 * **归属**
 * `app` 装配层：只决定顺序，不定义域状态。
 *
 * **口径**
 * 主题必须在外层。内层读主题时上下文已经就绪。
 */

/** **主题先于数据**：调换顺序会让数据层读不到主题上下文。 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </ThemeProvider>
  )
}
