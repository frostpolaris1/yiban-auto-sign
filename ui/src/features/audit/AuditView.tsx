/**
 * **功能**
 * 审计日志页容器：页头链状态结论 + 动作/操作者/起止筛选 + 游标分页台账 + 行详情抽屉。
 *
 * **归属**
 * `features/audit` 域容器：页面只引用它。
 *
 * **口径**
 * 权限由 `RUN_VIEW_ROLES`（master/admin）控制；user 渲染无权限卡片且零审计请求。
 * 页头结论复用 `lib/audit-status` 的三态判定：完好 / 异常 / 暂无行；
 * 断链时页头出危险告警，列表照常可用；列表读取失败只影响列表区，结论保持。
 * 筛选变化一律回第一页（游标清空）；翻页只带游标，末页 `next_cursor` 为空时下一页禁用。
 * 空态区分「无记录」与「筛选无命中」；详情只在行抽屉里展开，列表不撑破栅格。
 * 读屏播报点在列表卡头部常驻（视觉隐藏的 `aria-live="polite"`），内容同分页条；
 * 分页条本身只在数据态渲染，播报点若跟着它挂载，首屏读完读屏会漏读。
 */

import { useEffect, useRef, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { AsyncState } from '@/components/ui/AsyncState'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { Icon } from '@/components/ui/Icon'
import { PageHeader } from '@/components/ui/PageHeader'
import type { AuditLogItem, AuditStatusReport } from '@/lib/api/audit'
import { getAuditStatus, listAuditLogs } from '@/lib/api/audit'
import {
  AUDIT_VERDICT_LABELS,
  auditBrokenText,
  auditVerdict,
  auditVerdictTone,
  shortHash,
} from '@/lib/audit-status'
import {
  currentCursor,
  emptyCursorStack,
  goNext,
  goPrevious,
  hasPreviousPage,
  type CursorStack,
} from '@/lib/cursor'
import { formatDateTime } from '@/lib/format'
import { RUN_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { SHOWCASE, useMe } from '@/lib/session'
import { showcaseAuditLogs, showcaseAuditStatus } from '@/lib/showcase'
import { AuditDetail } from './AuditDetail'
import { AuditFilters } from './AuditFilters'
import {
  AUDIT_PAGE_SIZE,
  auditDayEnd,
  auditDayStart,
  auditErrorDetail,
  auditRowKey,
  detailPreview,
  formatAuditPager,
} from './display'
import { useAuditFilters } from './useAuditFilters'
import './audit.css'

const EMPTY_LOGS: AuditLogItem[] = []

/** 巡检是整链复算，不随每次翻页重取。 */
const STATUS_STALE_MS = 60_000

const COLUMN_HEADERS = ['时间', '动作', '操作者', '对象', '详情'] as const

type AuditVerdictProps = {
  report: AuditStatusReport | undefined
  loading: boolean
  error: string | null
  errorCode: string | null
  onRetry: () => void
}

/** 页头结论：三态徽标 + 巡检摘要；读取失败只降级为页头错误，不连坐列表。 */
function AuditVerdict({ report, loading, error, errorCode, onRetry }: AuditVerdictProps) {
  if (error !== null) {
    return (
      <div className="a-verdict" data-audit-verdict="error">
        <Badge tone="danger">读取失败</Badge>
        <span className="a-verdict-meta" title={errorCode ? `错误码：${errorCode}` : error}>
          {error}
        </span>
        <Button variant="ghost" size="sm" onClick={onRetry}>
          重试
        </Button>
      </div>
    )
  }

  if (report === undefined) {
    return (
      <div className="a-verdict" data-audit-verdict="loading">
        <Badge tone="info">{loading ? '巡检中…' : '等待巡检…'}</Badge>
      </div>
    )
  }

  const verdict = auditVerdict(report)
  const meta =
    verdict === 'empty'
      ? '链内还没有可校验的行；空链不算通过'
      : `巡检 ${report.checked_rows} 行 · ${formatDateTime(report.checked_at)}${
          report.last_hash === '' ? '' : ` · 链头 ${shortHash(report.last_hash)}`
        }`

  return (
    <div className="a-verdict" data-audit-verdict={verdict}>
      <Badge tone={auditVerdictTone(verdict)} dot>
        {AUDIT_VERDICT_LABELS[verdict]}
      </Badge>
      <span className="a-verdict-meta" title={meta}>
        {meta}
      </span>
    </div>
  )
}

export function AuditView() {
  const meQuery = useMe()
  const role = meQuery.data?.role ?? null
  const allowed = roleAllowed(role, RUN_VIEW_ROLES)

  // 游标栈与会话同寿命；任何筛选提交都把栈重置回第一页。
  const [stack, setStack] = useState<CursorStack>(() => emptyCursorStack())
  // 关闭抽屉只清 item，不清触发元素：焦点要回到打开它的按钮/行。
  const [detailItem, setDetailItem] = useState<AuditLogItem | null>(null)
  const [detailTrigger, setDetailTrigger] = useState<HTMLElement | null>(null)
  const tableScrollRef = useRef<HTMLDivElement | null>(null)
  const cardRef = useRef<HTMLElement | null>(null)
  const mountedRef = useRef(false)

  const filters = useAuditFilters(() => setStack(emptyCursorStack()))
  const cursor = currentCursor(stack)

  // 翻页或筛选变化后：表格横向滚动归零；翻页时列表回到视口顶部（输入防抖时不强制滚动视口，避免输入抖动）。
  useEffect(() => {
    if (!mountedRef.current) return
    if (tableScrollRef.current) tableScrollRef.current.scrollLeft = 0
  }, [stack.index, filters.action, filters.actor, filters.from, filters.to])

  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true
      return
    }
    cardRef.current?.scrollIntoView({ block: 'start', behavior: 'auto' })
  }, [stack.index])

  const statusQuery = useQuery({
    queryKey: ['audit-status'],
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseAuditStatus()) : getAuditStatus()),
    enabled: allowed,
    staleTime: STATUS_STALE_MS,
    retry: false,
  })

  const logsQuery = useQuery({
    queryKey: [
      'audit-logs',
      filters.action,
      filters.actor,
      filters.from,
      filters.to,
      cursor ?? null,
    ],
    queryFn: () => {
      const query = {
        action: filters.action || undefined,
        actor: filters.actor || undefined,
        from: auditDayStart(filters.from),
        to: auditDayEnd(filters.to),
        cursor,
        limit: AUDIT_PAGE_SIZE,
      }
      if (SHOWCASE) return Promise.resolve(showcaseAuditLogs(query))
      return listAuditLogs(query)
    },
    enabled: allowed,
    staleTime: 0,
    placeholderData: keepPreviousData,
    retry: false,
  })

  if (meQuery.isSuccess && !allowed) {
    return (
      <DeniedCard
        eyebrow="系统 · 审计"
        title="无权访问"
        reason="你的角色无权查看审计日志"
      />
    )
  }

  const report = statusQuery.data
  const verdict = report ? auditVerdict(report) : null
  const brokenText = report ? (auditBrokenText(report) ?? '审计链校验未通过。') : null
  const statusError = statusQuery.isError ? auditErrorDetail(statusQuery.error) : null
  const listError = logsQuery.isError ? auditErrorDetail(logsQuery.error) : null
  const rows = logsQuery.data?.items ?? EMPTY_LOGS
  const total = logsQuery.data?.total
  const hasNext = Boolean(logsQuery.data?.next_cursor)
  const filtered = filters.hasActive
  const showPager =
    !logsQuery.isError && !logsQuery.isPending && (rows.length > 0 || hasPreviousPage(stack))

  return (
    <div className="a-page">
      <PageHeader
        eyebrow="系统 · 审计"
        title={
          <>
            审计 <span className="accent">日志</span>
          </>
        }
        subtitle="审计链的只读台账；按动作、操作者与时间范围筛选。"
        actions={
          <AuditVerdict
            report={report}
            loading={statusQuery.isPending}
            error={statusError?.message ?? null}
            errorCode={statusError?.code ?? null}
            onRetry={() => {
              void statusQuery.refetch()
            }}
          />
        }
      />

      {verdict === 'broken' ? (
        <div className="a-chain-alert">
          <Alert tone="danger" title="审计链异常">
            {brokenText} 请保留现场并排查；下方台账仍可查阅。
          </Alert>
        </div>
      ) : null}

      <section className="a-card" aria-labelledby="audit-list-title" ref={cardRef}>
        <header className="a-card-head">
          <div className="a-card-title-wrap">
            <span className="a-dot is-primary" aria-hidden="true" />
            <div className="a-card-title-row">
              <h2 id="audit-list-title" className="a-card-title">
                审计行
              </h2>
              {total !== undefined ? (
                <span className="a-count-pill" data-audit-total>
                  {total}
                </span>
              ) : null}
              <span className="a-card-desc">按时间倒序 · 每页 {AUDIT_PAGE_SIZE} 条</span>
              {logsQuery.isFetching && !logsQuery.isPending ? (
                <span className="spinner sm" aria-label="刷新中" />
              ) : null}
            </div>
          </div>

          <span className="app-caption">游标分页</span>
          {/* 读屏播报点：常驻挂载，整句随翻页重读。
              分页条本身只在数据态出现，播报点若跟着它挂载，首屏读完读屏会漏读。 */}
          <span className="app-visually-hidden" aria-live="polite" aria-atomic="true">
            {showPager ? formatAuditPager(stack.index, rows.length) : ''}
          </span>
        </header>

        <div className="a-toolbar">
          <AuditFilters controls={filters} />
        </div>

        {logsQuery.isError ? (
          <div
            className="a-state"
            title={listError?.code ? `错误码：${listError.code}` : undefined}
          >
            <AsyncState
              status="error"
              error={listError?.message}
              onRetry={() => {
                void logsQuery.refetch()
              }}
              children={null}
            />
          </div>
        ) : logsQuery.isPending ? (
          <div className="a-state">
            <AsyncState status="loading" loadingLabel="加载审计行…" children={null} />
          </div>
        ) : rows.length === 0 ? (
          filters.error !== null ? (
            <div className="a-empty" role="status">
              <div className="a-empty-icon">
                <Icon name="alertCircle" size={18} />
              </div>
              <p className="a-empty-title">日期范围有误</p>
              <p className="a-empty-desc">先修正起止日期再筛选。</p>
            </div>
          ) : stack.index > 0 ? (
            <div className="a-empty" role="status">
              <div className="a-empty-icon">
                <Icon name="inbox" size={18} />
              </div>
              <p className="a-empty-title">本页无记录，可能已被删除</p>
              <p className="a-empty-desc">记录可能已不在当前筛选范围内。</p>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setStack(emptyCursorStack())}
              >
                回第一页
              </Button>
            </div>
          ) : (
            <div className="a-empty" role="status">
              <div className="a-empty-icon">
                <Icon name={filtered ? 'search' : 'shield'} size={18} />
              </div>
              <p className="a-empty-title">
                {filtered ? '当前筛选无匹配记录' : '暂无审计记录'}
              </p>
              <p className="a-empty-desc">
                {filtered
                  ? '换个动作、操作者或时间范围再试。'
                  : '审计链写入第一行后，记录会显示在这里。'}
              </p>
            </div>
          )
        ) : (
          <div
            className="a-table-scroll"
            ref={tableScrollRef}
            tabIndex={0}
            role="region"
            aria-labelledby="audit-list-title"
          >
            <table className="a-table">
              <caption className="app-visually-hidden">审计行列表</caption>
              <thead>
                <tr className="a-list-head">
                  {COLUMN_HEADERS.map((header) => (
                    <th key={header} scope="col">
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>

              <tbody className="a-list">
                {rows.map((item) => (
                  <tr
                    key={auditRowKey(item)}
                    className="a-row-wrap a-row"
                    data-audit-row
                    data-audit-id={item.id}
                    tabIndex={-1}
                    onClick={(event) => {
                      // 行本身可点开详情；点内层按钮或用户拖拽选中文本时不触发。
                      if ((event.target as HTMLElement).closest('button, a, input')) return
                      if ((window.getSelection()?.toString() ?? '') !== '') return
                      setDetailItem(item)
                      setDetailTrigger(event.currentTarget)
                    }}
                  >
                    <td className="a-time">{formatDateTime(item.at)}</td>
                    <td className="a-action a-mono" title={item.action}>
                      {item.action}
                    </td>
                    <td className="a-actor a-mono" title={item.actor}>
                      {item.actor === '' ? '—' : item.actor}
                    </td>
                    <td className="a-target a-mono" title={item.target}>
                      {item.target === '' ? '—' : item.target}
                    </td>
                    <td className="a-detail-cell">
                      <span
                        className="a-detail-preview a-mono"
                        title={item.detail === '' ? undefined : item.detail}
                      >
                        {detailPreview(item.detail)}
                      </span>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="a-detail-btn"
                        aria-label={`查看详情 #${item.id}`}
                        onClick={(event) => {
                          setDetailItem(item)
                          setDetailTrigger(event.currentTarget)
                        }}
                      >
                        详情
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {showPager ? (
          <footer className="a-pager-foot" data-audit-pager>
            <span className="a-pager-info">{formatAuditPager(stack.index, rows.length)}</span>
            <nav className="a-pager" aria-label="审计日志分页">
              <button
                type="button"
                className="a-pager-btn"
                disabled={!hasPreviousPage(stack) || logsQuery.isFetching}
                onClick={() => setStack((previous) => goPrevious(previous))}
              >
                <Icon name="chevronLeft" size={14} />
                上一页
              </button>
              <button
                type="button"
                className="a-pager-btn"
                disabled={!hasNext || logsQuery.isFetching}
                onClick={() => {
                  const next = logsQuery.data?.next_cursor
                  if (!next) return
                  setStack((previous) => goNext(previous, next))
                }}
              >
                下一页
                <Icon name="chevronRight" size={14} />
              </button>
            </nav>
          </footer>
        ) : null}
      </section>

      <AuditDetail
        item={detailItem}
        returnFocusTo={detailTrigger}
        onClose={() => setDetailItem(null)}
      />
    </div>
  )
}
