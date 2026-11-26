/**
 * **功能**
 * 底部系统健康与审计状态条：汇合执行引擎实时脉冲与审计链三态结论。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 `DashboardView` 注入。
 *
 * **口径**
 * 端点 `GET /api/admin/engine` 的 `x-roles` 为 master/admin；
 * `GET /api/admin/audit-status` 仅 master。admin 不请求、也不渲染审计巡检区。
 * 不在 user 视角挂载。
 * 正常态浓缩为页尾单行状态条，不再抢占首屏 KPI 视线；
 * 断链或锚点不符时在条下展开行内 danger 告警条，不弹窗、不展示审计正文。
 * 引擎读取失败与审计读取失败各自独立重试，互不连坐。
 */

import { useId, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { PauseBanner } from '@/components/ui/PauseBanner'
import type { AuditStatusReport } from '@/lib/api/audit'
import type { EngineSnapshot } from '@/lib/api/engine'
import { formatDateTime } from '@/lib/format'
import {
  AUDIT_VERDICT_LABELS,
  auditBrokenText,
  auditVerdict,
  auditVerdictTone,
  healthForcedKey,
  resolveHealthOpen,
  shortHash,
} from './audit-status'
import { enginePulse } from './display'

/** 暂停开关的动作面：状态、当前角色可否操作与置灰原因。 */
export type PauseAction = {
  paused: boolean
  canToggle: boolean
  blockedReason?: string
  onToggle: (opener?: HTMLElement | null) => void
}

type AuditStatusCardProps = {
  /** 审计巡检仅 master；false 时不渲染审计区（admin 零请求）。 */
  auditAllowed: boolean
  report: AuditStatusReport | undefined
  loading: boolean
  error: string | null
  onRetry: () => void
  engine: EngineSnapshot | undefined
  engineLoading: boolean
  engineError: string | null
  onRetryEngine: () => void
  pause?: PauseAction
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
  auditAllowed,
  report,
  loading,
  error,
  onRetry,
  engine,
  engineLoading,
  engineError,
  onRetryEngine,
  pause,
}: AuditStatusCardProps) {
  const pulse = enginePulse(engine)
  const broken = report ? auditBrokenText(report) : null
  const verdict = report === undefined ? null : auditVerdict(report)
  const auditText =
    verdict !== null
      ? AUDIT_VERDICT_LABELS[verdict]
      : loading
        ? '巡检中…'
        : '等待巡检…'
  const engineFailed = engineError !== null
  const auditFailed = auditAllowed && error !== null
  // 异常态强制展开；手动收起只对「当前这一起异常」有效：异常签名变化（新断链/失败）
  // 后 override 失配、回到强制展开，避免一次收起把后续告警永久折叠（R6）。
  // 恢复后若再次读到同文本异常，视为同一事件，保留用户此前的收起选择。
  const forcedKey = healthForcedKey(broken, error, engineError)
  const forcedOpen = (auditAllowed && broken !== null) || auditFailed || engineFailed
  const [override, setOverride] = useState<{ key: string; open: boolean } | null>(null)
  const open = resolveHealthOpen(forcedOpen, forcedKey, override)
  const panelId = useId()

  return (
    <section
      className="card app-health-strip"
      aria-label="系统健康与审计巡检"
      data-expanded={open}
    >
      <div className="app-health-summary">
        <span className="app-health-summary-label">引擎</span>
        <Badge tone={engineFailed ? 'danger' : pulse.tone} dot>
          {engineFailed ? '读取失败' : engineLoading && engine === undefined ? '读取中…' : pulse.label}
        </Badge>
        {auditAllowed ? (
          <>
            <span className="app-health-summary-label">审计</span>
            <Badge
              tone={auditFailed ? 'danger' : verdict === null ? 'info' : auditVerdictTone(verdict)}
            >
              {auditFailed ? '读取失败' : auditText}
            </Badge>
          </>
        ) : null}
        <button
          type="button"
          className="app-health-toggle"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOverride({ key: forcedKey, open: !open })}
        >
          {open ? '收起' : '详情'}
        </button>
      </div>
      <div className="app-health-detail" id={panelId}>
      {pause?.paused ? (
        <PauseBanner
          inflight={pulse.inflight ?? 0}
          dueNow={pulse.dueNow ?? 0}
          canResume={pause.canToggle}
          resumeBlockedReason={pause.blockedReason}
          onResume={pause.onToggle}
        />
      ) : null}
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
              <Link
                to="/logs"
                search={{ tab: 'patrol' }}
                className="app-health-chip app-health-chip-link"
              >
                在飞 <strong>{pulse.inflight ?? '—'}</strong>
              </Link>
              <Link
                to="/logs"
                search={{ tab: 'patrol' }}
                className="app-health-chip app-health-chip-link"
              >
                待执行 <strong>{pulse.dueNow ?? '—'}</strong>
              </Link>
              <Link
                to="/logs"
                search={{ tab: 'patrol' }}
                className="app-health-chip app-health-chip-link"
              >
                待重试 <strong>{pulse.retryingText}</strong>
              </Link>
              <span className="app-health-sub">
                {pulse.lastTickClock ? (
                  <span className="app-health-meta">最近一轮 {pulse.lastTickClock}</span>
                ) : null}
                <Link to="/logs" search={{ tab: 'events' }} className="app-health-link">
                  运行台账 →
                </Link>
                {pause && !pause.paused ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="app-health-action"
                    disabled={!pause.canToggle}
                    title={pause.canToggle ? undefined : pause.blockedReason}
                    onClick={(event) => pause.onToggle(event.currentTarget)}
                  >
                    暂停引擎
                  </Button>
                ) : null}
              </span>
            </div>
          )}
        </div>

        {auditAllowed ? (
          <>
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
          </>
        ) : null}
      </div>

      {auditAllowed && broken !== null ? (
        <div className="app-health-alert">
          <Alert tone="danger" title="审计异常">
            {broken} 请保留现场并排查；本页不展示审计正文。
          </Alert>
        </div>
      ) : null}
      </div>
    </section>
  )
}
