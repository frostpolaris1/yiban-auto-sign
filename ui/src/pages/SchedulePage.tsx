/**
 * **功能**
 * 时间片管理页：只挂载时间片域容器。
 *
 * **归属**
 * `pages` 路由页：页面逻辑留在 `features/schedule`。
 *
 * **口径**
 * 路径 `/schedule` 与 `nav.ts` 的叶子一一对应。
 * 业务日来自查询串；切换日期由路由组件写回查询串。
 * user 直接访问时由容器渲染无权限卡片，不发 schedule 请求。
 */

import { ScheduleView } from '@/features/schedule/ScheduleView'

type SchedulePageProps = {
  day: string | undefined
  onDayChange: (day: string) => void
}

export function SchedulePage({ day, onDayChange }: SchedulePageProps) {
  return <ScheduleView day={day} onDayChange={onDayChange} />
}
