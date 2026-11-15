/**
 * **功能**
 * 看板双图卡：30 天签到趋势（全宽）与自选时间片拥挤度（半宽，与容量体检并排）。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 DashboardView 给。
 *
 * **口径**
 * 只画契约给的字段；空数据不画空坐标轴，给一句原因。
 * 状态分布环形图已移除，避免与 30 天趋势重复同一份统计。
 * 自选图只在该任务存在时取数；无可用任务时给空态文案，不发请求。
 * 错误只挂本卡，重试只重取本卡。
 */

import { useCallback, type ReactNode } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import type { PrefStatsReport, SignStatsReport } from '@/lib/api/stats'
import type { ChartPalette } from './charts'
import { prefConfig, prefSummary, trendConfig, trendTotals } from './charts'
import { ChartCanvas } from './ChartCanvas'

type CardState = {
  loading: boolean
  error: string | null
  onRetry: () => void
}

function CardShell({
  state,
  children,
  empty,
  className,
  eyebrow,
  title,
  action,
}: {
  state: CardState
  children: ReactNode
  empty: string | null
  className: string
  eyebrow: string
  title: string
  action?: ReactNode
}) {
  return (
    <Card className={className} eyebrow={eyebrow} title={title} action={action}>
      {state.error !== null ? (
        <div className="app-alert-block">
          <Alert tone="danger" title="读取失败">
            {state.error}{' '}
            <Button variant="ghost" size="sm" onClick={state.onRetry}>
              重试
            </Button>
          </Alert>
        </div>
      ) : state.loading ? (
        <p className="app-muted">加载中…</p>
      ) : empty !== null ? (
        <p className="app-muted">{empty}</p>
      ) : (
        children
      )}
    </Card>
  )
}

type SignReportProps = {
  report: SignStatsReport | undefined
} & CardState

/** 30 天签到趋势：按日堆叠柱。 */
export function SignTrendCard({ report, loading, error, onRetry }: SignReportProps) {
  const totals = report ? trendTotals(report) : null
  const build = useCallback(
    (palette: ChartPalette) => trendConfig(report, palette),
    [report],
  )
  const empty =
    report && totals && totals.total === 0 ? '近 30 天暂无签到记录。' : null
  return (
    <CardShell
      className="col-12"
      eyebrow="Trend"
      title="30 天签到趋势"
      action={
        report ? <span className="app-caption">覆盖活跃账号 {report.total} 个</span> : null
      }
      state={{ loading, error, onRetry }}
      empty={empty}
    >
      <div className="app-chart-box">
        <ChartCanvas ariaLabel="近 30 天每日签到终态堆叠柱状图" build={build} />
      </div>
      {totals ? (
        <p className="app-muted app-chart-foot">
          30 天累计：成功 {totals.success} 次 · 失败 {totals.failed} 次 · 跳过 {totals.skipped} 次
          {totals.missing > 0 ? ` · 缺失 ${totals.missing} 天` : ''}
        </p>
      ) : null}
    </CardShell>
  )
}

type PrefReportProps = {
  report: PrefStatsReport | undefined
  taskLabel: string | null
} & CardState

/** 自选时间片拥挤度：半宽与容量体检并排，柱=人数，虚线=每片容量。 */
export function PrefCrowdingCard({ report, taskLabel, loading, error, onRetry }: PrefReportProps) {
  const build = useCallback(
    (palette: ChartPalette) => prefConfig(report, palette),
    [report],
  )
  const summary = report ? prefSummary(report) : null
  const cap = report?.slots[0]?.cap ?? 0
  const empty =
    taskLabel === null
      ? '暂无可用任务，暂不统计自选人数。'
      : report && report.slots.length === 0
        ? '当前窗口没有可展示的时间片。'
        : null
  return (
    <CardShell
      className="col-6"
      eyebrow="Crowding"
      title="签到时段拥挤度"
      action={taskLabel ? <Badge tone="primary">{taskLabel}</Badge> : null}
      state={{ loading, error, onRetry }}
      empty={empty}
    >
      <div className="app-chart-box app-chart-box--compact">
        <ChartCanvas ariaLabel="自选时间片人数与每片容量对比图" build={build} />
      </div>
      {summary ? (
        <p className="app-muted app-chart-foot">
          {summary.users > 0
            ? `已自选 ${summary.users} 人 · 高峰 ${summary.hottest?.label ?? '—'}${
                summary.hottest ? `（${summary.hottest.count} 人）` : ''
              } · 每片上限 ${cap} 人`
            : `暂无用户自选，按自动铺点 · 每片上限 ${cap} 人`}
          {' · 15 秒刷新'}
        </p>
      ) : null}
    </CardShell>
  )
}
