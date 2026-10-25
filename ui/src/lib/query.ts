import { QueryClient } from '@tanstack/react-query'

/**
 * Query client defaults.
 *
 * There is no global polling interval. A page that needs polling sets
 * `refetchInterval` on its own query. Admin live data will use SSE from P6.
 * No query runs in this batch.
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
