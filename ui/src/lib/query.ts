import { QueryClient } from '@tanstack/react-query'

/**
 * Query client defaults.
 *
 * User-side pages poll every 30–60 seconds (master plan §3.5.3). Admin live
 * data will use SSE from P6, not polling. No query runs in this batch.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchInterval: 45_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})
