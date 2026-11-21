/**
 * **功能**
 * 用户端历史路由页：只装配 `features/me` 的历史容器。
 *
 * **归属**
 * `pages/user-app` 路由页：查询串由路由层解析后传入。
 *
 * **口径**
 * `?day=YYYY-MM-DD` 是选中日；非法值在路由层丢弃，页面回落到今天。
 */

import { HistoryView } from '@/features/me/HistoryView'

export function UserHistoryPage({
  day,
  onDayChange,
}: {
  day: string | undefined
  onDayChange: (day: string) => void
}) {
  return <HistoryView day={day} onDayChange={onDayChange} />
}
