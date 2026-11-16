/**
 * **功能**
 * 仪表盘页：页头加按角色的看板装配。
 *
 * **归属**
 * `pages` 路由页：只装配，不承载域逻辑。
 *
 * **口径**
 * 看板三档角色都可见；取数面与卡片由 `DashboardView` 按角色决定。
 * 时间偏好卡只对 user 渲染：端点 `x-roles` 只有 user，master/admin 不挂载即不请求。
 * 页头不放动作：建号入口只在账号页，旧线习惯（用户裁决）。
 * 副标题如实描述数据来源，不写占位或演示字样。
 */

import { PageHeader } from '@/components/ui/PageHeader'
import { DashboardView } from '@/features/dashboard/DashboardView'
import { TimePrefCard } from '@/features/schedule/TimePrefCard'
import { useMe } from '@/lib/session'

export function DashboardPage() {
  const meQuery = useMe()
  const role = meQuery.data?.role ?? null
  const isUser = role === 'user'

  return (
    <>
      <PageHeader
        eyebrow="今日 · 总览"
        title={
          <>
            签到运行 <span className="accent">总览</span>
          </>
        }
        subtitle={
          isUser
            ? '今日签到状态与诊断建议；状态每 30 秒自动刷新。'
            : '今日签到终态、任务容量水位与 30 天趋势总览。'
        }
      />

      {role === null ? null : (
        <DashboardView role={role} extraCards={isUser ? <TimePrefCard /> : null} />
      )}
    </>
  )
}
