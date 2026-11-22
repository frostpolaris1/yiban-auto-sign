/**
 * **功能**
 * 仪表盘页：页头加按角色的看板装配。
 *
 * **归属**
 * `pages` 路由页：只装配，不承载域逻辑。
 *
 * **口径**
 * 总览归管理台 master/admin；user 不再进壳，由 AppShell 一律 302 到用户端 `/app`。
 * 时间偏好卡归用户端 App：本页不再按角色装配额外卡片。
 * 页头不放动作：建号入口只在账号页，旧线习惯（用户裁决）。
 * 副标题如实描述数据来源，不写占位或演示字样。
 */

import { PageHeader } from '@/components/ui/PageHeader'
import { DashboardView } from '@/features/dashboard/DashboardView'
import { useMe } from '@/lib/session'

export function DashboardPage() {
  const meQuery = useMe()
  const role = meQuery.data?.role ?? null

  return (
    <>
      <PageHeader
        eyebrow="今日 · 总览"
        title={
          <>
            签到运行 <span className="accent">总览</span>
          </>
        }
        subtitle="今日签到终态、任务容量水位与 30 天趋势总览。"
      />

      {role === null ? null : <DashboardView role={role} />}
    </>
  )
}
