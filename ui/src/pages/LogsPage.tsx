/**
 * **功能**
 * 签到台账页：签到事件（摘要、泳道、历史检索）与运行巡检两页签一页。
 *
 * **归属**
 * `pages` 路由页：把路由的 search 与写回传给 `features/logs.LogsView`。
 * 页面逻辑（取数、状态、装配）都在 feature 里，本文件只做参数搬运。
 *
 * **口径**
 * 只归 master 与 admin；准入判据在 `lib/roles.RUN_VIEW_ROLES`，
 * 无权限由 LogsView 渲染无权限卡片，导航项不渲染。
 * search 的解析与序列化都在 `features/logs/urlState.ts`。
 */

import { LogsView } from '@/features/logs/LogsView'
import type { RunURLState } from '@/features/logs/urlState'

type LogsPageProps = {
  search: RunURLState
  onSearchChange: (next: RunURLState, opts?: { replace?: boolean }) => void
}

export function LogsPage({ search, onSearchChange }: LogsPageProps) {
  return <LogsView urlState={search} onSearchChange={onSearchChange} />
}
