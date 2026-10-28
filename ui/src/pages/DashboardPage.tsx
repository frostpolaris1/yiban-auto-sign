/**
 * **功能**
 * 仪表盘占位页：页头、提示、KPI、两张卡，以及 user 专属的时间偏好卡。
 *
 * **归属**
 * `pages` 路由页：只装配，不承载域逻辑。
 *
 * **口径**
 * 占位数字不发请求，保证视觉基线稳定。数据接入安排在调度与实时批次。
 * 时间偏好卡只对 user 渲染：端点 `x-roles` 只有 user，master/admin 不挂载即不请求。
 */

import { Icon } from '@/components/ui/Icon'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { PageHeader } from '@/components/ui/PageHeader'
import { CapacityCard } from '@/features/dashboard/CapacityCard'
import { KpiGrid } from '@/features/dashboard/KpiGrid'
import { SignStatusCard } from '@/features/dashboard/SignStatusCard'
import { TimePrefCard } from '@/features/schedule/TimePrefCard'
import { useMe } from '@/lib/session'

/**
 * 全部演示值。**改数字会让视觉基线失效**。
 * 真实数据接入前，此页只定 Adminator 视觉基线。
 */
export function DashboardPage() {
  const meQuery = useMe()
  const isUser = meQuery.data?.role === 'user'

  return (
    <>
      <PageHeader
        eyebrow="工作区 · 概览"
        title={
          <>
            签到运行 <span className="accent">总览</span>
          </>
        }
        subtitle="占位仪表盘。数据接入安排在调度与实时批次；本页只固定 Adminator 视觉基线。"
        actions={
          <>
            <Button variant="ghost">
              <Icon name="download" />
              导出
            </Button>
            <Button variant="primary">
              <Icon name="plus" />
              新建账号
            </Button>
          </>
        }
      />

      <div className="app-alert-block">
        <Alert tone="info" title="占位数据">
          本批不调用后端。所有数字与状态均为演示值，不代表真实签到结果。
        </Alert>
      </div>

      <KpiGrid />

      <div className="grid">
        <SignStatusCard />
        <CapacityCard />
        {isUser ? <TimePrefCard /> : null}
      </div>
    </>
  )
}
