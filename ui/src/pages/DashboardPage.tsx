/**
 * **功能**
 * 仪表盘占位页：页头、提示、KPI、两张卡。
 *
 * **归属**
 * `pages` 路由页：只装配，不承载域逻辑。
 *
 * **口径**
 * 本批不发请求。数字固定，保证视觉基线稳定。
 * 数据接入安排在调度与实时批次。
 */

import { Icon } from '@/components/ui/Icon'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { PageHeader } from '@/components/ui/PageHeader'
import { CapacityCard } from '@/features/dashboard/CapacityCard'
import { KpiGrid } from '@/features/dashboard/KpiGrid'
import { SignStatusCard } from '@/features/dashboard/SignStatusCard'

/**
 * 全部数据是演示值。**改数字会让视觉基线失效**。
 * 真实数据接入前，此页只定 Adminator 视觉基线。
 */
export function DashboardPage() {
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
      </div>
    </>
  )
}
