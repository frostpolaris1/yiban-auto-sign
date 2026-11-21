/**
 * **功能**
 * 台账域的实时刷新钩子（重导出共享实现 `@/lib/adminEvents`）。
 *
 * **归属**
 * `features/logs` 域钩子：供台账容器与页签引用。
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
