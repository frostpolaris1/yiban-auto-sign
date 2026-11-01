/**
 * **功能**
 * 执行监控页：单进程执行面的只读快照。
 *
 * **归属**
 * `pages` 路由页：把路由装配交给 `features/run.EngineView`。
 * 页面逻辑（取数、状态、装配）都在 feature 里，本文件只挂载。
 *
 * **口径**
 * 只归 master 与 admin；准入判据在 `lib/roles.ENGINE_VIEW_ROLES`，
 * 无权限由 EngineView 渲染无权限卡片，导航项不渲染。
 * 本页不设查询串：快照无分页与筛选，URL 没有状态可保。
 */

import { EngineView } from '@/features/run/EngineView'

export function EnginePage() {
  return <EngineView />
}
