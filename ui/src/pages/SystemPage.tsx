/**
 * **功能**
 * 系统设置路由页：只挂载系统设置域容器。
 *
 * **归属**
 * `pages` 路由页：页面逻辑留在 `features/system`。
 *
 * **口径**
 * 路径 `/system` 与 `nav.ts` 的叶子一一对应。
 * 业务日来自查询串；切换日期由路由组件写回查询串。
 * user 直接访问时由容器渲染无权限卡片，零业务请求。
 */

import { SystemView } from '@/features/system/SystemView'

type SystemPageProps = {
  day: string | undefined
  onDayChange: (day: string) => void
}

export function SystemPage({ day, onDayChange }: SystemPageProps) {
  return <SystemView day={day} onDayChange={onDayChange} />
}
