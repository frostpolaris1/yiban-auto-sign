import { QueryClient } from '@tanstack/react-query'

/**
 * **功能**
 * 查询客户端缺省值：缓存 30 秒、失焦不重取、失败重试 1 次。
 *
 * **归属**
 * `lib` 工具层：只造客户端，不发请求。
 *
 * **口径**
 * 本文件不设全局轮询。需要轮询的页面自己给 `refetchInterval`。
 * 管理端实时数据走 P6 的 SSE。本批没有任何查询运行。
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})
