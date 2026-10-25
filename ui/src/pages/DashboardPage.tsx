import { Icon } from '@/components/ui/Icon'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { PageHeader } from '@/components/ui/PageHeader'
import { CapacityCard } from '@/features/dashboard/CapacityCard'
import { KpiGrid } from '@/features/dashboard/KpiGrid'
import { SignStatusCard } from '@/features/dashboard/SignStatusCard'

/**
 * Placeholder dashboard.
 * No request is made in this batch. Numbers are fixed so the visual baseline
 * is stable.
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
