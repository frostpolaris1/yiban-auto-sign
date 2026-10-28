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
 * 副标题如实描述数据来源，不写占位或演示字样。
 */

import { Icon } from '@/components/ui/Icon'
import { Button } from '@/components/ui/Button'
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
        eyebrow="工作区 · 概览"
        title={
          <>
            签到运行 <span className="accent">总览</span>
          </>
        }
        subtitle={
          isUser
            ? '今日签到状态与诊断建议；状态每 30 秒自动刷新。'
            : '今日任务窗口与容量体检；容量按所选任务计算。'
        }
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

      {role === null ? null : (
        <DashboardView role={role} extraCards={isUser ? <TimePrefCard /> : null} />
      )}
    </>
  )
}
