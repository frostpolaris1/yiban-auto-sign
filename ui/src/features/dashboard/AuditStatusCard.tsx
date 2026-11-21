/**
 * **功能**
 * 底部系统健康与审计状态条：汇合执行引擎实时脉冲与审计链三态结论。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 `DashboardView` 注入。
 *
 * **口径**
 * 端点 `GET /api/admin/engine` 与 `GET /api/admin/audit-status` 的 `x-roles`
 * 均为 master/admin；不在 user 视角挂载。
 * 正常态浓缩为页尾单行状态条，不再抢占首屏 KPI 视线；
 * 断链或锚点不符时在条下展开行内 danger 告警条，不弹窗、不展示审计正文。
 * 引擎读取失败与审计读取失败各自独立重试，互不连坐。
 */

import { Link } from '@tanstack/react-router'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import type { AuditStatusReport } from '@/lib/api/audit'
import type { EngineSnapshot } from '@/lib/api/engine'
import { formatDateTime } from '@/lib/format'
import {
  AUDIT_VERDICT_LABELS,
  auditBrokenText,
  auditVerdict,
  auditVerdictTone,
  shortHash,
} from './audit-status'
import { enginePulse } from './display'

type AuditStatusCardProps = {
  report: AuditStatusReport | undefined
  loading: boolean
  error: string | null
  onRetry: () => void
  engine: EngineSnapshot | undefined
  engineLoading: boolean
  engineError: string | null
  onRetryEngine: () => void
}

function AuditSummaryInline({ report }: { report: AuditStatusReport }) {
  const verdict = auditVerdict(report)
  const anchorNote =
    report.anchor_status === 'ok' ||
    report.anchor_status === 'mismatch' ||
    report.anchor_status === 'error'
      ? ` · 库外锚点 ${report.anchor_status}${
          report.anchor_seq === undefined ? '' : ` · seq ${report.anchor_seq}`
        }`
      : report.anchor_status === 'missing'
        ? ' · 锚点未启用（仅链内自洽）'
        : ''

  return (
    <div className="app-health-inline">
      <Badge tone={auditVerdictTone(verdict)}>{AUDIT_VERDICT_LABELS[verdict]}</Badge>
      <span className="app-health-sub">
        <span className="app-health-meta">
        {verdict === 'empty' ? (
          <>链内还没有可校验的行；空链不算通过{anchorNote}</>
        ) : (
          <>
            巡检 {report.checked_rows} 行 · {formatDateTime(report.checked_at)}
            {report.last_hash === '' ? '' : ` · 链头 ${shortHash(report.last_hash)}`}
            {anchorNote}
          </>
        )}
        </span>
      </span>
    </div>
  )
}

export function AuditStatusCard({
  report,
  loading,
  error,
  onRetry,
  engine,
  engineLoading,
  engineError,
  onRetryEngine,
}: AuditStatusCardProps) {
  const pulse = enginePulse(engine)
  const broken = report ? auditBrokenText(report) : null

  return (
    <section className="card app-health-strip" aria-label="系统健康与审计巡检">
      <div className="app-health-bar">
        <div className="app-health-zone">
          <span className="app-health-label">
            <Icon name="activity" size={14} />
            执行引擎
          </span>
          {engineError !== null ? (
            <div className="app-health-inline">
              <Badge tone="danger">读取失败</Badge>
              <span className="app-health-meta">{engineError}</span>
              <Button variant="ghost" size="sm" onClick={onRetryEngine}>
                重试
              </Button>
            </div>
          ) : (
            <div className="app-health-inline">
              <Badge tone={pulse.tone} dot>
                {engineLoading && engine === undefined ? '读取中…' : pulse.label}
              </Badge>
              <Link to="/engine" className="app-health-chip app-health-chip-link">
                在飞 <strong>{pulse.inflight ?? '—'}</strong>
              </Link>
              <Link to="/engine" className="app-health-chip app-health-chip-link">
                待执行 <strong>{pulse.dueNow ?? '—'}</strong>
              </Link>
              <Link to="/engine" className="app-health-chip app-health-chip-link">
                待重试 <strong>{pulse.retryingText}</strong>
              </Link>
              <span className="app-health-sub">
                {pulse.lastTickClock ? (
                  <span className="app-health-meta">最近一轮 {pulse.lastTickClock}</span>
                ) : null}
                <Link to="/run" className="app-health-link">运行台账 →</Link>
              </span>
            </div>
          )}
        </div>

        <span className="app-health-sep" aria-hidden="true" />

        <div className="app-health-zone app-health-zone--grow">
          <span className="app-health-label">
            <Icon name="shield" size={14} />
            审计链巡检
          </span>
          {error !== null ? (
            <div className="app-health-inline">
              <Badge tone="danger">读取失败</Badge>
              <span className="app-health-meta">{error}</span>
              <Button variant="ghost" size="sm" onClick={onRetry}>
                重试
              </Button>
            </div>
          ) : report === undefined ? (
            <span className="app-health-meta">{loading ? '巡检中…' : '等待巡检…'}</span>
          ) : (
            <AuditSummaryInline report={report} />
          )}
        </div>
      </div>

      {broken !== null ? (
        <div className="app-health-alert">
          <Alert tone="danger" title="审计异常">
            {broken} 请保留现场并排查；本页不展示审计正文。
          </Alert>
        </div>
      ) : null}
    </section>
  )
}
