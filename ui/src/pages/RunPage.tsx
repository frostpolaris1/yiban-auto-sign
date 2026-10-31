/**
 * **功能**
 * 运行页：今日轮次摘要、任务泳道与历史检索三段一页。
 *
 * **归属**
 * `pages` 路由页：把路由的 search 与写回传给 `features/run.RunView`。
 * 页面逻辑（取数、状态、装配）都在 feature 里，本文件只做参数搬运。
 *
 * **口径**
 * 只归 master 与 admin；准入判据在 `lib/roles.RUN_VIEW_ROLES`，
 * 无权限由 RunView 渲染无权限卡片，导航项不渲染。
 * search 的解析与序列化都在 `features/run/urlState.ts`。
 */

import { RunView } from '@/features/run/RunView'
import type { RunURLState } from '@/features/run/urlState'

type RunPageProps = {
  search: RunURLState
  onSearchChange: (next: RunURLState, opts?: { replace?: boolean }) => void
}

export function RunPage({ search, onSearchChange }: RunPageProps) {
  return <RunView urlState={search} onSearchChange={onSearchChange} />
}
