/**
 * **功能**
 * 运行域的实时刷新钩子（重导出共享实现 `@/lib/adminEvents`）。
 *
 * **归属**
 * `features/run` 域钩子：保持既有导入路径兼容。
 */

export {
  DEFAULT_END_REASON,
  RUN_LIVE_STATUS_LABELS,
  RUN_POLL_INTERVAL_MS,
  useRunLive,
  type EventSourceFactory,
  type RunLive,
  type RunLiveOptions,
  type RunLiveStatus,
} from '@/lib/adminEvents'
