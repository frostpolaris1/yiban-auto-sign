/**
 * **功能**
 * 审计链巡检卡：三态徽章、巡检行数与时刻、断链定位行。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调查由 `DashboardView` 给。
 *
 * **口径**
 * 端点 `GET /api/admin/audit-status` 的 `x-roles` 是 master/admin；
 * 本卡不自行取数，也不在 user 视角挂载。
 * 三态口径见 `audit-status.ts`：空链是「暂无审计行」，不是异常。
 * 断链只给定位行 id；**不展示**审计正文、actor 或 detail。
 * 异常用行内 danger 徽章与提示条，不弹窗。
 */

import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import type { AuditStatusReport } from '@/lib/api/audit'
import { formatDateTime } from '@/lib/format'
import {
  AUDIT_VERDICT_LABELS,
  auditBrokenText,
  auditVerdict,
  auditVerdictTone,
  shortHash,
} from './audit-status'

type AuditStatusCardProps = {
  report: AuditStatusReport | undefined
  loading: boolean
  error: string | null
  onRetry: () => void
}

/** 巡检结论正文：三态徽章、行数与时刻、断链定位。 */
function AuditStatusDetail({ report }: { report: AuditStatusReport }) {
  const verdict = auditVerdict(report)
  const broken = auditBrokenText(report)
  return (
    <>
      <p>
        <Badge tone={auditVerdictTone(verdict)}>{AUDIT_VERDICT_LABELS[verdict]}</Badge>{' '}
        {verdict === 'ok' ? (
          <span>全部行已复算通过。</span>
        ) : verdict === 'empty' ? (
          <span>链内还没有可校验的行；空链不算通过。</span>
        ) : null}
      </p>
      <p className="app-muted">
        巡检 {report.checked_rows} 行 · {formatDateTime(report.checked_at)}
        {report.last_hash === '' ? '' : ` · 链头 ${shortHash(report.last_hash)}`}
      </p>
      {report.anchor_status === 'ok' ||
      report.anchor_status === 'mismatch' ||
      report.anchor_status === 'error' ? (
        <p className="app-muted">
          库外锚点：{report.anchor_status}
          {report.anchor_seq === undefined ? '' : ` · seq ${report.anchor_seq}`}
          {report.anchor_count === undefined ? '' : ` · ${report.anchor_count} 行`}
        </p>
      ) : null}
      {report.anchor_status === 'missing' ? (
        <Alert tone="warning" title="锚点未启用">
          巡检仅链内自洽。
        </Alert>
      ) : null}
      {broken !== null ? (
        <Alert tone="danger" title="审计异常">
          {broken} 请保留现场并排查；本页不展示审计正文。
        </Alert>
      ) : null}
    </>
  )
}

export function AuditStatusCard({
  report,
  loading,
  error,
  onRetry,
}: AuditStatusCardProps) {
  return (
    <Card
      className="col-12"
      eyebrow="Audit"
      title="审计链巡检"
      action={<span className="app-caption">按需复算全链</span>}
    >
      {error !== null ? (
        <div className="app-alert-block">
          <Alert tone="danger" title="读取失败">
            {error}{' '}
            <Button variant="ghost" size="sm" onClick={onRetry}>
              重试
            </Button>
          </Alert>
        </div>
      ) : report === undefined ? (
        <p className="app-muted">{loading ? '巡检中…' : '等待巡检…'}</p>
      ) : (
        <AuditStatusDetail report={report} />
      )}
    </Card>
  )
}
