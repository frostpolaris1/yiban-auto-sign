/**
 * **功能**
 * 今日失败明细卡：把今日终态失败按结果码拆开，给条数、占比与处置建议。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 `DashboardView` 注入。
 *
 * **口径**
 * 数据只来自 `GET /api/sign-stats/failures`（按失败结果分桶的已有端点），
 * 不重读 `/api/sign-stats`：趋势卡与 KPI 已经共用那一次读取，本卡不得再+1。
 * 不传 `day`：业务日由服务端按部署时区判定，本卡原样回显 `report.day`，
 * 避免前端时区与服务端不一致时把昨天的结论标成今天。
 * 服务端按条数降序排好，本卡不重排；只对空结果与词表外的码做兜底文案。
 * 空态必须说清「今天没有失败」与「读取失败」不是一回事。
 * 错误只挂本卡，重试只重取本卡。
 */

import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import type { SignFailuresReport } from '@/lib/api/stats'
import {
  FAILURE_EMPTY_HINT,
  FAILURE_EMPTY_TEXT,
  failureBucketRows,
} from './failure-display'

type FailureBreakdownCardProps = {
  report: SignFailuresReport | undefined
  loading: boolean
  error: string | null
  onRetry: () => void
}

const COLUMNS = 4

export function FailureBreakdownCard({
  report,
  loading,
  error,
  onRetry,
}: FailureBreakdownCardProps) {
  const rows = report === undefined ? [] : failureBucketRows(report)
  const caption =
    report === undefined ? '今日 · 终态失败' : `${report.day} · 共 ${report.total} 条`

  return (
    <Card
      className="col-12"
      eyebrow="Failures"
      title="今日失败明细"
      action={<span className="app-caption">{caption}</span>}
    >
      <div className="table-scroll">
        <table className="table">
          <caption className="app-visually-hidden">今日失败明细</caption>
          <thead>
            <tr>
              <th scope="col">失败结果</th>
              <th scope="col">条数</th>
              <th scope="col">占比</th>
              <th scope="col">处置建议</th>
            </tr>
          </thead>
          <tbody>
            {error !== null ? (
              <tr>
                <td colSpan={COLUMNS}>
                  <span className="app-muted">{error}</span>{' '}
                  <Button variant="ghost" size="sm" onClick={onRetry}>
                    重试
                  </Button>
                </td>
              </tr>
            ) : null}

            {error === null && loading && rows.length === 0 ? (
              <tr>
                <td colSpan={COLUMNS} className="app-muted">
                  加载中…
                </td>
              </tr>
            ) : null}

            {error === null && !loading && rows.length === 0 ? (
              <tr>
                <td colSpan={COLUMNS}>
                  <span className="app-muted">{FAILURE_EMPTY_TEXT}</span>{' '}
                  <span className="app-caption">{FAILURE_EMPTY_HINT}</span>
                </td>
              </tr>
            ) : null}

            {rows.map((row) => (
              <tr key={row.code === '' ? 'unknown' : row.code}>
                <td className="cell-name">
                  {row.label}{' '}
                  <span className="data-cell-mono">{row.code === '' ? 'unknown' : row.code}</span>
                </td>
                <td className="data-cell-mono">{row.count} 条</td>
                <td className="data-cell-mono">{row.share}</td>
                <td>{row.hint}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
