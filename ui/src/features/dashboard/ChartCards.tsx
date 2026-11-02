/**
 * **功能**
 * 看板三张图卡：30 天趋势、状态分布、自选时间片拥挤度。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 DashboardView 给。
 *
 * **口径**
 * 只画契约给的字段；空数据不画空坐标轴，给一句原因。
 * 分布图按状态去重，桶间可重叠，摘要明确写「不相加」。
 * 自选图只在有选中任务时挂载；无任务时不发请求。
 * 错误只挂本卡，重试只重取本卡。
 */

import { useCallback, type ReactNode } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import type { PrefStatsReport, SignStatsReport } from '@/lib/api/stats'
import type { ChartPalette } from './charts'
import { distributionConfig, prefConfig, prefSummary, trendConfig, trendTotals } from './charts'
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
      className="col-6"
      eyebrow="Trend"
      title="30 天签到趋势"
      state={{ loading, error, onRetry }}
      empty={empty}
    >
      <div className="app-chart-box">
        <ChartCanvas ariaLabel="近 30 天每日签到终态堆叠柱状图" build={build} />
      </div>
      {totals ? (
        <p className="app-muted">
          近 30 天：成功 {totals.success} · 失败 {totals.failed} · 跳过 {totals.skipped}
          {totals.missing > 0 ? ` · 缺失 ${totals.missing} 天` : ''}
        </p>
      ) : null}
    </CardShell>
  )
}

/** 状态分布：窗口内按状态去重的账号数。 */
export function SignDistributionCard({ report, loading, error, onRetry }: SignReportProps) {
  const build = useCallback(
    (palette: ChartPalette) => distributionConfig(report, palette),
    [report],
  )
  const hasData =
    report !== undefined &&
    Object.values(report.by_status).some((value) => value > 0)
  const empty = report && !hasData ? '窗口内暂无账号终态。' : null
  return (
    <CardShell
      className="col-6"
      eyebrow="Mix"
      title="状态分布"
      state={{ loading, error, onRetry }}
      empty={empty}
    >
      <div className="app-chart-box">
        <ChartCanvas ariaLabel="窗口内账号终态分布环形图" build={build} />
      </div>
      {report ? (
        <p className="app-muted">
          按状态去重共 {report.total} 个账号；各状态桶可重叠，不相加。
        </p>
      ) : null}
    </CardShell>
  )
}

type PrefReportProps = {
  report: PrefStatsReport | undefined
  taskLabel: string | null
} & CardState

/** 自选时间片拥挤度：柱=人数，虚线=每片容量。 */
export function PrefCrowdingCard({ report, taskLabel, loading, error, onRetry }: PrefReportProps) {
  const build = useCallback(
    (palette: ChartPalette) => prefConfig(report, palette),
    [report],
  )
  const summary = report ? prefSummary(report) : null
  const cap = report?.slots[0]?.cap ?? 0
  const empty = report && report.slots.length === 0 ? '当前窗口没有可展示的时间片。' : null
  return (
    <CardShell
      className="col-12"
      eyebrow="Crowding"
      title="签到时段拥挤度"
      action={taskLabel ? <span className="app-caption">{taskLabel}</span> : null}
      state={{ loading, error, onRetry }}
      empty={empty}
    >
      <div className="app-chart-box app-chart-box--wide">
        <ChartCanvas ariaLabel="自选时间片人数与每片容量对比图" build={build} />
      </div>
      {summary ? (
        <p className="app-muted">
          {summary.users > 0
            ? `自选用户 ${summary.users} · 最热 ${summary.hottest?.label ?? '—'}${
                summary.hottest ? `（${summary.hottest.count} 人）` : ''
              } · 每片容量 ${cap}`
            : `暂无用户自选，按自动铺点；每片容量 ${cap}`}
        </p>
      ) : null}
    </CardShell>
  )
}
