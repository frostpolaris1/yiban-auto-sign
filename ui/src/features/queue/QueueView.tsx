/**
 * **功能**
 * 待办工作台容器：顶部遥测摘要 + 三组独立分页队列（待审核 / 今日失败 / 今日未签）+
 * SSE 实时推送（防抖合并失效）+ 驳回理由弹窗 + 对象诊断抽屉。
 *
 * **归属**
 * `features/queue` 域容器：页面只引用它。
 *
 * **口径**
 * 权限由 `RUN_VIEW_ROLES`（master / admin）控制；user 渲染无权限卡片且零业务请求。
 * 复用 `['schools']`、`['schedule-windows', day]` 与 `['account-summary']` 共享缓存键，
 * 避免跨页与侧栏重复取数；翻页与切换分段使用 `keepPreviousData` 消除闪烁与跳变。
 * 实时流连接 `/api/admin/events`（展示模式除外），300ms 尾随合并突发事件并精准失效受影响组；
 * 断线期间页面可见时每 10 秒轮询任务组。
 */

import { useEffect, useRef, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { Icon } from '@/components/ui/Icon'
import { notifySuccess } from '@/components/ui/toast'
import {
  DEFAULT_END_REASON,
  RUN_LIVE_STATUS_LABELS,
  RUN_POLL_INTERVAL_MS,
  useRunLive,
} from '@/lib/adminEvents'
import {
  getAccountSummary,
  listAccounts,
  reviewAccount,
  signAccount,
  type Account,
} from '@/lib/api/account'
import { describeApiError } from '@/lib/api/client'
import { listScheduleWindows } from '@/lib/api/schedule'
import { listAllSchools, type School } from '@/lib/api/school'
import { listTasks, type AdminTaskRow } from '@/lib/api/task'
import {
  currentCursor,
  emptyCursorStack,
  goNext,
  goPrevious,
  hasPreviousPage,
  type CursorStack,
} from '@/lib/cursor'
import { dayOf, formatClock, formatDateTime } from '@/lib/format'
import { RUN_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { SHOWCASE, useMe } from '@/lib/session'
import {
  showcaseAccountSummary,
  showcaseAllSchools,
  showcaseQueueAccounts,
  showcaseQueueTasks,
  showcaseWindows,
} from '@/lib/showcase'
import {
  QUEUE_PAGE_SIZE,
  describeTaskResult,
  formatWindowSummary,
  schoolNameMap,
  taskStateMeta,
  type ReviewSegment,
  type UnsignedSegment,
} from './queueDisplay'
import { QueueDiagnosticsDrawer, type QueueDiagnosticsTarget } from './QueueDiagnosticsDrawer'
import { QueueGroup } from './QueueGroup'
import { QueueRejectModal } from './QueueRejectModal'
import { QueueTelemetryStrip } from './QueueTelemetryStrip'
import { useQueueFilters } from './useQueueFilters'
import './queue.css'

const EMPTY_ACCOUNTS: Account[] = []
const EMPTY_TASKS: AdminTaskRow[] = []
const EMPTY_SCHOOLS: School[] = []
const READ_ONLY_REASON = '展示模式只读'

const SCHOOLS_STALE_MS = 5 * 60_000
const WINDOWS_STALE_MS = 60_000
const SUMMARY_STALE_MS = 10_000
const SSE_COALESCE_MS = 300

function RowErrorBanner({
  message,
  onDismiss,
}: {
  message: string
  onDismiss: () => void
}) {
  const [closing, setClosing] = useState(false)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    }
  }, [])

  const handleDismiss = () => {
    const reduced =
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      onDismiss()
      return
    }
    setClosing(true)
    timerRef.current = window.setTimeout(onDismiss, 160)
  }

  return (
    <div className="q-row-error" role="alert" data-closing={closing ? 'true' : undefined}>
      <span className="q-row-error-text">
        <Icon name="alertCircle" size={14} />
        {message}
      </span>
      <button
        type="button"
        className="mail-tool"
        aria-label="关闭行错误提示"
        onClick={handleDismiss}
      >
        <Icon name="close" size={14} />
      </button>
    </div>
  )
}

export function QueueView() {
  const queryClient = useQueryClient()
  const meQuery = useMe()
  const role = meQuery.data?.role ?? null
  const allowed = roleAllowed(role, RUN_VIEW_ROLES)

  // 上海业务日每分钟复核一次，跨日自动换键重取
  const [today, setToday] = useState(() => dayOf(new Date()))
  useEffect(() => {
    const timer = window.setInterval(() => {
      setToday(dayOf(new Date()))
    }, 60_000)
    return () => window.clearInterval(timer)
  }, [])

  // 三组独立游标栈与分段状态
  const [reviewSegment, setReviewSegment] = useState<ReviewSegment>('pending')
  const [reviewStack, setReviewStack] = useState<CursorStack>(() => emptyCursorStack())

  const [failedStack, setFailedStack] = useState<CursorStack>(() => emptyCursorStack())

  const [unsignedSegment, setUnsignedSegment] = useState<UnsignedSegment>('pending')
  const [unsignedStack, setUnsignedStack] = useState<CursorStack>(() => emptyCursorStack())

  // 行内错误与弹窗/抽屉目标
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const [rejectTarget, setRejectTarget] = useState<{
    account: Account
    trigger: HTMLElement | null
  } | null>(null)
  const [rejectError, setRejectError] = useState<string | null>(null)
  const [diagTarget, setDiagTarget] = useState<{
    target: QueueDiagnosticsTarget
    trigger: HTMLElement | null
  } | null>(null)

  // SSE 实时推送：300ms 尾随合并 + 按事件精准失效缓存（展示模式禁用）
  const live = useRunLive(queryClient, allowed && !SHOWCASE, undefined, {
    coalesceMs: SSE_COALESCE_MS,
    onInvalidate: (eventTypes) => {
      const hasWindow = eventTypes.includes('window.changed')
      const hasFullSync =
        eventTypes.includes('resync') ||
        eventTypes.includes('snapshot') ||
        eventTypes.includes('reconnect') ||
        eventTypes.includes('end')
      if (hasFullSync) {
        void queryClient.invalidateQueries({ queryKey: ['queue'] })
        void queryClient.invalidateQueries({ queryKey: ['schedule-windows', today] })
        void queryClient.invalidateQueries({ queryKey: ['account-summary'] })
        return
      }
      if (hasWindow) {
        void queryClient.invalidateQueries({ queryKey: ['schedule-windows', today] })
      }
      if (eventTypes.includes('task.progress') || eventTypes.includes('task.concluded')) {
        void queryClient.invalidateQueries({ queryKey: ['queue', 'tasks'] })
      }
    },
  })

  // SSE 断线且页签可见时，每 10 秒降级轮询任务列表
  useEffect(() => {
    if (!allowed || SHOWCASE || live.status !== 'disconnected') return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        void queryClient.invalidateQueries({ queryKey: ['queue', 'tasks'] })
      }
    }, RUN_POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [allowed, live.status, queryClient])

  const reviewCursor = currentCursor(reviewStack)
  const failedCursor = currentCursor(failedStack)
  const unsignedCursor = currentCursor(unsignedStack)

  // 组内筛选：学校 + 关键字各自独立，提交即回第一页
  const reviewFilters = useQueueFilters(() => setReviewStack(emptyCursorStack()))
  const failedFilters = useQueueFilters(() => setFailedStack(emptyCursorStack()))
  const unsignedFilters = useQueueFilters(() => setUnsignedStack(emptyCursorStack()))

  const summaryQuery = useQuery({
    queryKey: ['account-summary'],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseAccountSummary()) : getAccountSummary(),
    enabled: allowed,
    staleTime: SUMMARY_STALE_MS,
    retry: false,
  })

  const windowsQuery = useQuery({
    queryKey: ['schedule-windows', today],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseWindows(today)) : listScheduleWindows({ day: today }),
    enabled: allowed,
    staleTime: WINDOWS_STALE_MS,
    retry: false,
  })

  const schoolsQuery = useQuery({
    queryKey: ['schools'],
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseAllSchools()) : listAllSchools()),
    enabled: allowed,
    staleTime: SCHOOLS_STALE_MS,
    retry: false,
  })

  const reviewQuery = useQuery({
    queryKey: [
      'queue',
      'accounts',
      reviewSegment,
      reviewFilters.schoolId,
      reviewFilters.q,
      reviewCursor ?? null,
    ],
    queryFn: () =>
      SHOWCASE
        ? Promise.resolve(
            showcaseQueueAccounts(reviewSegment, {
              schoolId: reviewFilters.schoolId,
              q: reviewFilters.q,
            }),
          )
        : listAccounts({
            status: reviewSegment,
            school_id: reviewFilters.schoolId || undefined,
            q: reviewFilters.q || undefined,
            cursor: reviewCursor,
            limit: QUEUE_PAGE_SIZE,
          }),
    enabled: allowed,
    staleTime: 0,
    placeholderData: keepPreviousData,
    retry: false,
  })

  const failedQuery = useQuery({
    queryKey: [
      'queue',
      'tasks',
      'failed',
      today,
      failedFilters.schoolId,
      failedFilters.q,
      failedCursor ?? null,
    ],
    queryFn: () =>
      SHOWCASE
        ? Promise.resolve(
            showcaseQueueTasks('failed', {
              schoolId: failedFilters.schoolId,
              q: failedFilters.q,
            }),
          )
        : listTasks({
            day: today,
            state: 'failed',
            school_id: failedFilters.schoolId || undefined,
            q: failedFilters.q || undefined,
            cursor: failedCursor,
            limit: QUEUE_PAGE_SIZE,
          }),
    enabled: allowed,
    staleTime: 0,
    placeholderData: keepPreviousData,
    retry: false,
  })

  const unsignedQuery = useQuery({
    queryKey: [
      'queue',
      'tasks',
      'unsigned',
      unsignedSegment,
      today,
      unsignedFilters.schoolId,
      unsignedFilters.q,
      unsignedCursor ?? null,
    ],
    queryFn: () =>
      SHOWCASE
        ? Promise.resolve(
            showcaseQueueTasks(unsignedSegment, {
              schoolId: unsignedFilters.schoolId,
              q: unsignedFilters.q,
            }),
          )
        : listTasks({
            day: today,
            state: unsignedSegment,
            school_id: unsignedFilters.schoolId || undefined,
            q: unsignedFilters.q || undefined,
            cursor: unsignedCursor,
            limit: QUEUE_PAGE_SIZE,
          }),
    enabled: allowed,
    staleTime: 0,
    placeholderData: keepPreviousData,
    retry: false,
  })

  const clearRowError = (key: string) => {
    setRowErrors((prev) => {
      if (!(key in prev)) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }

  const setRowError = (key: string, message: string) => {
    setRowErrors((prev) => ({ ...prev, [key]: message }))
  }

  const approveMutation = useMutation({
    mutationFn: (account: Account) => reviewAccount(account.id, { decision: 'approve' }),
    onSuccess: (updated) => {
      clearRowError(updated.id)
      notifySuccess(`已通过 ${updated.display_name || updated.phone_masked}`)
      void queryClient.invalidateQueries({ queryKey: ['queue', 'accounts'] })
      void queryClient.invalidateQueries({ queryKey: ['account-summary'] })
    },
    onError: (error, account) => {
      setRowError(account.id, describeApiError(error))
    },
  })

  const rejectMutation = useMutation({
    mutationFn: ({ account, reason }: { account: Account; reason: string }) =>
      reviewAccount(account.id, { decision: 'reject', reason }),
    onSuccess: (updated) => {
      clearRowError(updated.id)
      setRejectTarget(null)
      setRejectError(null)
      notifySuccess(`已驳回 ${updated.display_name || updated.phone_masked}`)
      void queryClient.invalidateQueries({ queryKey: ['queue', 'accounts'] })
      void queryClient.invalidateQueries({ queryKey: ['account-summary'] })
    },
    onError: (error) => {
      setRejectError(describeApiError(error))
    },
  })

  const retryMutation = useMutation({
    mutationFn: (task: AdminTaskRow) => signAccount(task.account_id),
    onSuccess: (_accepted, task) => {
      clearRowError(task.id)
      notifySuccess(`已触发重签 ${task.account_phone_masked}`)
      void queryClient.invalidateQueries({ queryKey: ['queue', 'tasks', 'failed'] })
    },
    onError: (error, task) => {
      setRowError(task.id, describeApiError(error))
    },
  })

  if (meQuery.isSuccess && !allowed) {
    return <DeniedCard eyebrow="今日 · 待办" title="无权访问" reason="你的角色无权查看待办队列" />
  }

  const schools = schoolsQuery.data ?? EMPTY_SCHOOLS
  const schoolsById = schoolNameMap(schools)
  const reviewRows = reviewQuery.data?.items ?? EMPTY_ACCOUNTS
  const failedRows = failedQuery.data?.items ?? EMPTY_TASKS
  const unsignedRows = unsignedQuery.data?.items ?? EMPTY_TASKS

  const windowSummary = windowsQuery.data ? formatWindowSummary(windowsQuery.data.items) : null
  const pendingTotal = summaryQuery.data
    ? summaryQuery.data.pending + summaryQuery.data.rejected
    : null
  const pendingDetail = summaryQuery.data
    ? `待审 ${summaryQuery.data.pending} · 已驳回 ${summaryQuery.data.rejected}`
    : null

  const refreshingAny =
    reviewQuery.isFetching ||
    failedQuery.isFetching ||
    unsignedQuery.isFetching ||
    windowsQuery.isFetching ||
    summaryQuery.isFetching

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['queue'] })
    void queryClient.invalidateQueries({ queryKey: ['schedule-windows', today] })
    void queryClient.invalidateQueries({ queryKey: ['account-summary'] })
  }

  const jumpToGroup = (id: string) => {
    const element = document.getElementById(id)
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }

  return (
    <div className="q-page">
      <header className="q-header">
        <span className="eyebrow q-header-eyebrow">今日 · 待办</span>
        <div className="q-header-row">
          <h1 className="hero-title q-header-title">
            今日 <span className="accent">待办</span>
          </h1>
          <div className="q-header-actions">
            {!SHOWCASE ? (
              <span className="q-live-pill" role="status">
                <span
                  className={`q-dot ${
                    live.status === 'live'
                      ? 'is-success is-pulse'
                      : live.status === 'resyncing'
                        ? 'is-info is-pulse'
                        : 'is-warning'
                  }`}
                  aria-hidden="true"
                />
                {RUN_LIVE_STATUS_LABELS[live.status]}
              </span>
            ) : null}
            <Button
              variant="ghost"
              className="q-refresh-btn"
              onClick={refreshAll}
              disabled={refreshingAny}
            >
              <Icon
                name="refreshCw"
                size={13}
                className={refreshingAny ? 'q-spin' : undefined}
              />
              {refreshingAny ? '刷新中…' : '刷新'}
            </Button>
          </div>
        </div>
        <p className="hero-sub q-header-sub">账号审核、失败重试与未签处理。</p>
      </header>

      {!SHOWCASE && live.status !== 'live' ? (
        <div className="q-live-banner" role="status">
          <span>
            {live.status === 'resyncing'
              ? '正在同步最新状态…'
              : live.status === 'ended'
                ? `实时同步已结束：${live.endedReason ?? DEFAULT_END_REASON}`
                : `实时同步已断开（最近 ${live.since ?? '—'}），当前每 10 秒轮询。`}
          </span>
          {live.status !== 'resyncing' ? (
            <Button variant="outline-primary" size="sm" onClick={live.reconnect}>
              重连
            </Button>
          ) : null}
        </div>
      ) : null}

      <QueueTelemetryStrip
        day={windowsQuery.data?.day ?? today}
        windowSummary={windowSummary}
        windowError={windowsQuery.isError}
        pendingTotal={pendingTotal}
        pendingDetail={pendingDetail}
        failedCount={failedQuery.data ? failedRows.length : null}
        failedPartial={Boolean(failedQuery.data?.next_cursor)}
        unsignedCount={unsignedQuery.data ? unsignedRows.length : null}
        unsignedPartial={Boolean(unsignedQuery.data?.next_cursor)}
        unsignedLabel={unsignedSegment === 'pending' ? '今日待签' : '进行中'}
        onJumpTo={jumpToGroup}
      />

      {/* 第一组：待审核（待审核 / 已拒绝分段） */}
      <QueueGroup<ReviewSegment>
        id="queue-group-pending"
        groupKey="pending"
        title="待审核"
        description={reviewSegment === 'pending' ? '新提交账号' : '待用户修改重提'}
        tone="warning"
        badgeCount={
          reviewFilters.hasActive
            ? reviewQuery.data
              ? `${reviewQuery.data.next_cursor ? '≥' : ''}${reviewRows.length}`
              : null
            : summaryQuery.data
              ? reviewSegment === 'pending'
                ? summaryQuery.data.pending
                : summaryQuery.data.rejected
              : reviewQuery.data
                ? reviewRows.length
                : null
        }
        segments={[
          {
            value: 'pending',
            label: '待审核',
            count: summaryQuery.data?.pending ?? null,
          },
          {
            value: 'rejected',
            label: '已拒绝',
            count: summaryQuery.data?.rejected ?? null,
          },
        ]}
        activeSegment={reviewSegment}
        onSegmentChange={(next) => {
          if (next === reviewSegment) return
          setReviewSegment(next)
          setReviewStack(emptyCursorStack())
        }}
        schools={schools}
        selectedSchoolId={reviewFilters.schoolId}
        onSchoolChange={reviewFilters.setSchoolId}
        searchInput={reviewFilters.input}
        searchPlaceholder="备注名 / 手机号 / 邮箱"
        searchAriaLabel="搜索待审核账号"
        onSearchInputChange={reviewFilters.setInput}
        onSearchCommit={reviewFilters.commitNow}
        hasActiveFilters={reviewFilters.hasActive}
        onClearFilters={reviewFilters.clear}
        columnHeaders={[
          '账号与归属',
          '手机号',
          '学校',
          reviewSegment === 'pending' ? '提交时间' : '驳回理由',
          '操作',
        ]}
        pageIndex={reviewStack.index}
        rowCount={reviewRows.length}
        hasPrevious={hasPreviousPage(reviewStack)}
        hasNext={Boolean(reviewQuery.data?.next_cursor)}
        pagingDisabled={reviewQuery.isFetching}
        onPrevious={() => setReviewStack((prev) => goPrevious(prev))}
        onNext={() => {
          const nextCursor = reviewQuery.data?.next_cursor
          if (nextCursor) setReviewStack((prev) => goNext(prev, nextCursor))
        }}
        isLoading={reviewQuery.isPending}
        isFetching={reviewQuery.isFetching}
        error={reviewQuery.isError ? describeApiError(reviewQuery.error) : null}
        onRetry={() => {
          void reviewQuery.refetch()
        }}
        isEmpty={reviewRows.length === 0}
        emptyIcon="checkCircle"
        emptyTitle={reviewSegment === 'pending' ? '待审核已清空' : '暂无已拒绝账号'}
        emptyDescription={
          reviewSegment === 'pending' ? '暂无待审批账号' : '被驳回账号会显示在这里'
        }
      >
        {reviewRows.map((account) => {
          const schoolName = schoolsById.get(account.school_id) ?? account.school_id
          const approvingThis =
            approveMutation.isPending && approveMutation.variables?.id === account.id
          const rejectingThis =
            rejectMutation.isPending && rejectTarget?.account.id === account.id
          const busy = approvingThis || rejectingThis
          const rowError = rowErrors[account.id]
          const displayName = account.display_name || `账号${account.phone_masked.slice(-4)}`

          return (
            <li key={account.id} className="q-row-wrap" data-queue-row data-account-id={account.id}>
              <div className="q-row is-pending">
                <div className="q-col-primary">
                  <span className="q-row-title" title={displayName}>
                    {displayName}
                  </span>
                  <span className="q-row-sub q-mono-code" title={account.owner_email_masked}>
                    {account.owner_email_masked}
                  </span>
                </div>
                <div className="q-col-phone">
                  <span className="q-phone">{account.phone_masked}</span>
                </div>
                <div className="q-col-school">
                  <span className="q-school-tag" title={schoolName}>
                    {schoolName}
                  </span>
                </div>
                <div className="q-col-meta">
                  {account.status === 'rejected' && account.reject_reason ? (
                    <span className="q-row-reason" title={account.reject_reason}>
                      {account.reject_reason}
                    </span>
                  ) : (
                    <span className="q-row-time">{formatDateTime(account.created_at)}</span>
                  )}
                </div>

                <div className="q-row-actions">
                  {account.status === 'pending' ? (
                    <Button
                      variant="primary"
                      size="sm"
                      className="q-action-btn"
                      disabled={busy || SHOWCASE}
                      title={SHOWCASE ? READ_ONLY_REASON : undefined}
                      aria-label={`通过 ${account.phone_masked}`}
                      onClick={() => approveMutation.mutate(account)}
                    >
                      <Icon name="check" size={14} />
                      {approvingThis ? '处理中…' : '通过'}
                    </Button>
                  ) : null}

                  <Button
                    variant="ghost"
                    size="sm"
                    className="q-action-btn"
                    disabled={busy || SHOWCASE}
                    title={SHOWCASE ? READ_ONLY_REASON : undefined}
                    aria-label={`驳回 ${account.phone_masked}`}
                    onClick={(event) => {
                      setRejectError(null)
                      setRejectTarget({ account, trigger: event.currentTarget })
                    }}
                  >
                    驳回
                  </Button>

                  <Button
                    variant="ghost"
                    size="sm"
                    className="q-action-btn"
                    aria-label={`详情 ${account.phone_masked}`}
                    onClick={(event) =>
                      setDiagTarget({
                        target: {
                          accountId: account.id,
                          title: account.phone_masked,
                          subtitle: `${schoolName} · ${account.display_name || account.phone_masked}`,
                        },
                        trigger: event.currentTarget,
                      })
                    }
                  >
                    详情
                  </Button>
                </div>
              </div>

              {rowError ? (
                <RowErrorBanner
                  message={rowError}
                  onDismiss={() => clearRowError(account.id)}
                />
              ) : null}
            </li>
          )
        })}
      </QueueGroup>

      {/* 第二组：今日失败 */}
      <QueueGroup
        id="queue-group-failed"
        groupKey="failed"
        title="今日失败"
        description="终态失败，可重试"
        tone="danger"
        pulse={failedRows.length > 0}
        badgeCount={
          failedQuery.data
            ? `${failedQuery.data.next_cursor ? '≥' : ''}${failedRows.length}`
            : null
        }
        schools={schools}
        selectedSchoolId={failedFilters.schoolId}
        onSchoolChange={failedFilters.setSchoolId}
        searchInput={failedFilters.input}
        searchPlaceholder="任务名 / 学校 / 手机号"
        searchAriaLabel="搜索今日失败任务"
        onSearchInputChange={failedFilters.setInput}
        onSearchCommit={failedFilters.commitNow}
        hasActiveFilters={failedFilters.hasActive}
        onClearFilters={failedFilters.clear}
        columnHeaders={['任务与计划', '手机号', '学校', '失败原因与建议', '操作']}
        pageIndex={failedStack.index}
        rowCount={failedRows.length}
        hasPrevious={hasPreviousPage(failedStack)}
        hasNext={Boolean(failedQuery.data?.next_cursor)}
        pagingDisabled={failedQuery.isFetching}
        onPrevious={() => setFailedStack((prev) => goPrevious(prev))}
        onNext={() => {
          const nextCursor = failedQuery.data?.next_cursor
          if (nextCursor) setFailedStack((prev) => goNext(prev, nextCursor))
        }}
        isLoading={failedQuery.isPending}
        isFetching={failedQuery.isFetching}
        error={failedQuery.isError ? describeApiError(failedQuery.error) : null}
        onRetry={() => {
          void failedQuery.refetch()
        }}
        isEmpty={failedRows.length === 0}
        emptyIcon="inbox"
        emptyTitle="今日暂无失败"
        emptyDescription="今日未出现失败任务"
      >
        {failedRows.map((task) => {
          const retryingThis =
            retryMutation.isPending && retryMutation.variables?.id === task.id
          const rowError = rowErrors[task.id]
          const guide = describeTaskResult(task.result)

          return (
            <li key={task.id} className="q-row-wrap" data-queue-row data-task-id={task.id}>
              <div className="q-row is-failed">
                <div className="q-col-primary">
                  <span className="q-row-title" title={task.task_name}>
                    {task.task_name}
                  </span>
                  <span className="q-row-sub">
                    计划 {formatClock(task.run_at)} · 尝试 {task.attempts} 次
                  </span>
                </div>
                <div className="q-col-phone">
                  <span className="q-phone">{task.account_phone_masked}</span>
                </div>
                <div className="q-col-school">
                  <span className="q-school-tag" title={task.school_name}>
                    {task.school_name}
                  </span>
                </div>
                <div className="q-col-meta">
                  {task.result ? (
                    <>
                      <span className="q-reason-label" title={guide.hint}>
                        {guide.label}
                      </span>
                      <span className="q-row-sub q-mono-code">{task.result}</span>
                      <span className="q-reason-hint" title={guide.hint}>
                        {guide.hint}
                      </span>
                    </>
                  ) : (
                    <span className="q-row-time">—</span>
                  )}
                </div>

                <div className="q-row-actions">
                  <Button
                    variant="soft-primary"
                    size="sm"
                    className="q-action-btn"
                    disabled={retryingThis || SHOWCASE}
                    title={SHOWCASE ? READ_ONLY_REASON : undefined}
                    aria-label={`重试 ${task.account_phone_masked}`}
                    onClick={() => retryMutation.mutate(task)}
                  >
                    <Icon
                      name="rotateCcw"
                      size={14}
                      className={retryingThis ? 'q-spin' : undefined}
                    />
                    {retryingThis ? '重试中…' : '重试'}
                  </Button>

                  <Button
                    variant="ghost"
                    size="sm"
                    className="q-action-btn"
                    aria-label={`详情 ${task.account_phone_masked}`}
                    onClick={(event) =>
                      setDiagTarget({
                        target: {
                          accountId: task.account_id,
                          title: task.account_phone_masked,
                          subtitle: `${task.school_name} · ${task.task_name}`,
                        },
                        trigger: event.currentTarget,
                      })
                    }
                  >
                    详情
                  </Button>
                </div>
              </div>

              {rowError ? (
                <RowErrorBanner
                  message={rowError}
                  onDismiss={() => clearRowError(task.id)}
                />
              ) : null}
            </li>
          )
        })}
      </QueueGroup>

      {/* 第三组：今日未签（待签 / 进行中分段） */}
      <QueueGroup<UnsignedSegment>
        id="queue-group-unsigned"
        groupKey="unsigned"
        title="今日未签"
        description={unsignedSegment === 'pending' ? '等待计划时刻' : '正在执行'}
        tone={unsignedSegment === 'running' ? 'primary' : 'info'}
        pulse={unsignedSegment === 'running' && unsignedRows.length > 0}
        badgeCount={
          unsignedQuery.data
            ? `${unsignedQuery.data.next_cursor ? '≥' : ''}${unsignedRows.length}`
            : null
        }
        segments={[
          { value: 'pending', label: '待签' },
          { value: 'running', label: '进行中' },
        ]}
        activeSegment={unsignedSegment}
        onSegmentChange={(next) => {
          if (next === unsignedSegment) return
          setUnsignedSegment(next)
          setUnsignedStack(emptyCursorStack())
        }}
        schools={schools}
        selectedSchoolId={unsignedFilters.schoolId}
        onSchoolChange={unsignedFilters.setSchoolId}
        searchInput={unsignedFilters.input}
        searchPlaceholder="任务名 / 学校 / 手机号"
        searchAriaLabel="搜索今日未签任务"
        onSearchInputChange={unsignedFilters.setInput}
        onSearchCommit={unsignedFilters.commitNow}
        hasActiveFilters={unsignedFilters.hasActive}
        onClearFilters={unsignedFilters.clear}
        columnHeaders={['任务与状态', '手机号', '学校', '计划与认领', '操作']}
        pageIndex={unsignedStack.index}
        rowCount={unsignedRows.length}
        hasPrevious={hasPreviousPage(unsignedStack)}
        hasNext={Boolean(unsignedQuery.data?.next_cursor)}
        pagingDisabled={unsignedQuery.isFetching}
        onPrevious={() => setUnsignedStack((prev) => goPrevious(prev))}
        onNext={() => {
          const nextCursor = unsignedQuery.data?.next_cursor
          if (nextCursor) setUnsignedStack((prev) => goNext(prev, nextCursor))
        }}
        isLoading={unsignedQuery.isPending}
        isFetching={unsignedQuery.isFetching}
        error={unsignedQuery.isError ? describeApiError(unsignedQuery.error) : null}
        onRetry={() => {
          void unsignedQuery.refetch()
        }}
        isEmpty={unsignedRows.length === 0}
        emptyIcon="clock"
        emptyTitle={unsignedSegment === 'pending' ? '没有待签任务' : '暂无进行中任务'}
        emptyDescription={
          unsignedSegment === 'pending' ? '今日待签已全部处理' : '当前无执行中任务'
        }
      >
        {unsignedRows.map((task) => {
          const meta = taskStateMeta(task.state)

          return (
            <li key={task.id} className="q-row-wrap" data-queue-row data-task-id={task.id}>
              <div className="q-row is-unsigned">
                <div className="q-col-primary">
                  <span className="q-row-title" title={task.task_name}>
                    {task.task_name}
                  </span>
                  <Badge tone={meta.tone} dot>
                    {meta.label}
                  </Badge>
                </div>
                <div className="q-col-phone">
                  <span className="q-phone">{task.account_phone_masked}</span>
                </div>
                <div className="q-col-school">
                  <span className="q-school-tag" title={task.school_name}>
                    {task.school_name}
                  </span>
                </div>
                <div className="q-col-meta">
                  <span className="q-row-time">计划 {formatClock(task.run_at)}</span>
                  {task.claimed_at ? (
                    <span className="q-row-time">认领 {formatClock(task.claimed_at)}</span>
                  ) : task.attempts > 0 ? (
                    <span className="q-row-time">尝试 {task.attempts} 次</span>
                  ) : null}
                </div>

                <div className="q-row-actions">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="q-action-btn"
                    aria-label={`详情 ${task.account_phone_masked}`}
                    onClick={(event) =>
                      setDiagTarget({
                        target: {
                          accountId: task.account_id,
                          title: task.account_phone_masked,
                          subtitle: `${task.school_name} · ${task.task_name}`,
                        },
                        trigger: event.currentTarget,
                      })
                    }
                  >
                    详情
                  </Button>
                </div>
              </div>
            </li>
          )
        })}
      </QueueGroup>

      <QueueRejectModal
        account={rejectTarget?.account ?? null}
        schoolName={
          rejectTarget
            ? schoolsById.get(rejectTarget.account.school_id) ?? rejectTarget.account.school_id
            : ''
        }
        pending={rejectMutation.isPending}
        error={rejectError}
        readOnly={SHOWCASE}
        returnFocusTo={rejectTarget?.trigger ?? null}
        onClose={() => {
          setRejectTarget(null)
          setRejectError(null)
        }}
        onSubmit={(reason) => {
          if (!rejectTarget) return
          setRejectError(null)
          rejectMutation.mutate({ account: rejectTarget.account, reason })
        }}
      />

      <QueueDiagnosticsDrawer
        target={diagTarget?.target ?? null}
        returnFocusTo={diagTarget?.trigger ?? null}
        onClose={() => setDiagTarget(null)}
      />
    </div>
  )
}
