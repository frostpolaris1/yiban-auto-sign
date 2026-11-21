/**
 * **功能**
 * 签到事件页签：今日轮次摘要、按学校泳道、历史检索（筛选/CSV导出/游标分页）与行诊断抽屉。
 *
 * **归属**
 * `features/logs` 页签组件：由 `LogsView` 在 `tab=events` 时挂载。
 *
 * **口径**
 * URL 是筛选与游标的唯一事实源：`?tab&day&state&school&q&cursor`。
 * 摘要恒查今天（limit 200，next_cursor 非空即部分统计），读取失败给
 * 非阻塞提示与重试，不拖垮泳道与历史检索；
 * 学校筛选取 `GET /api/schools` 全量下拉，URL 存 id；
 * 泳道与历史表共用同一查询（当前筛选＋游标），不重复取数。
 * 搜索输入防抖 250ms；回车立即生效。任一筛选变化回到第一页。
 * 断线期间仅在本页签激活时每 10 秒轮询一次 `['run-page']`。
 */

import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AsyncState } from '@/components/ui/AsyncState'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { describeApiError } from '@/lib/api/client'
import { listAllSchools } from '@/lib/api/school'
import { exportTasksCsv, listTasks, type AdminTaskRow } from '@/lib/api/task'
import {
  currentCursor,
  emptyCursorStack,
  goNext,
  goPrevious,
  hasPreviousPage,
  type CursorStack,
} from '@/lib/cursor'
import { dayOf, formatClock } from '@/lib/format'
import { SHOWCASE } from '@/lib/session'
import { showcaseAllSchools, showcaseRunPage } from '@/lib/showcase'
import { runStateLabel, runStateTone } from './display'
import { RunDiagnosticsDrawer } from './RunDiagnosticsDrawer'
import { RunFilters } from './RunFilters'
import { RunSummary, runCounts } from './RunSummary'
import { SchoolLanes, lanesBySchool } from './SchoolLanes'
import {
  RUN_LIVE_STATUS_LABELS,
  RUN_POLL_INTERVAL_MS,
  type RunLiveStatus,
} from './useRunLive'
import { withRunFilter, type RunURLState } from './urlState'

/** 输入防抖窗口。与用户页同窗。 */
const SEARCH_DEBOUNCE_MS = 250

// 空列表用同一个常量：`?? []` 每次渲染都造新数组，会让下游 useMemo 失效。
const EMPTY_ROWS: AdminTaskRow[] = []

export type EventsTabProps = {
  urlState: RunURLState
  onSearchChange: (next: RunURLState, opts?: { replace?: boolean }) => void
  allowed: boolean
  liveStatus: RunLiveStatus
}

export function EventsTab({
  urlState,
  onSearchChange,
  allowed,
  liveStatus,
}: EventsTabProps) {
  const queryClient = useQueryClient()
  const today = dayOf(new Date())
  const day = urlState.day ?? today

  const [qInput, setQInput] = useState(urlState.q ?? '')
  // URL 是输入框的回灌源：后退清掉 ?q 时输入框必须同步清空，
  // 否则防抖会把旧值写回 URL，后退等于失效（用户页同款守卫）。
  const urlQuery = urlState.q
  useEffect(() => {
    setQInput(urlQuery ?? '')
  }, [urlQuery])

  // 游标栈只在会话内记「上一页」；URL 是当前页的游标事实源。
  const [stack, setStack] = useState<CursorStack>(() =>
    urlState.cursor ? { cursors: [undefined, urlState.cursor], index: 1 } : emptyCursorStack(),
  )
  const urlCursor = urlState.cursor
  useEffect(() => {
    setStack((previous) => {
      if (currentCursor(previous) === (urlCursor ?? undefined)) return previous
      return urlCursor ? { cursors: [undefined, urlCursor], index: 1 } : emptyCursorStack()
    })
  }, [urlCursor])
  const cursor = currentCursor(stack)
  const hasPrevious = hasPreviousPage(stack)

  /** 回车是显式动作：同值也回第一页。 */
  const commitQuery = () => {
    onSearchChange(withRunFilter(urlState, { q: qInput.trim() || undefined }))
  }

  useEffect(() => {
    const next = qInput.trim() || undefined
    if (next === (urlState.q ?? undefined)) return
    const timer = window.setTimeout(() => {
      onSearchChange(withRunFilter(urlState, { q: next }))
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [qInput, urlState, onSearchChange])

  const tasksQuery = useQuery({
    queryKey: [
      'run-page',
      'list',
      day,
      urlState.state ?? null,
      urlState.school ?? null,
      urlState.q ?? null,
      cursor ?? null,
    ],
    staleTime: 0,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseRunPage())
      return listTasks({
        day,
        state: urlState.state,
        school_id: urlState.school,
        q: urlState.q,
        cursor,
      })
    },
    enabled: allowed,
  })

  const summaryQuery = useQuery({
    queryKey: ['run-page', 'summary', today],
    staleTime: 0,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseRunPage())
      return listTasks({ day: today, limit: 200 })
    },
    enabled: allowed,
  })

  // 学校下拉的选项源：服务端按角色收敛（master 全量、admin 绑定范围）。
  // 键独立于 run-page：SSE/轮询按前缀失效 ['run-page'] 时不得连带重取学校。
  const schoolsQuery = useQuery({
    queryKey: ['schools', 'filter'],
    staleTime: 5 * 60_000,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseAllSchools())
      return listAllSchools()
    },
    enabled: allowed,
  })

  // 断线降级：仅在本页签激活期间，列表与摘要每 10 秒轮询一次。
  useEffect(() => {
    if (!allowed || SHOWCASE || liveStatus !== 'disconnected') return
    const timer = window.setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: ['run-page'] })
    }, RUN_POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [allowed, liveStatus, queryClient])

  const rows = tasksQuery.data?.items ?? EMPTY_ROWS
  const lanes = useMemo(() => lanesBySchool(rows), [rows])
  const summary = summaryQuery.data
    ? runCounts(summaryQuery.data.items, summaryQuery.data.next_cursor !== '')
    : null

  const listStatus: 'loading' | 'error' | 'empty' | 'data' = tasksQuery.isPending
    ? 'loading'
    : tasksQuery.isError
      ? 'error'
      : rows.length === 0
        ? 'empty'
        : 'data'
  const error = tasksQuery.isError ? describeApiError(tasksQuery.error) : null

  const [diagRow, setDiagRow] = useState<AdminTaskRow | null>(null)
  const [diagReturnFocus, setDiagReturnFocus] = useState<HTMLElement | null>(null)
  const openDiagnostics = (row: AdminTaskRow, opener: HTMLElement) => {
    setDiagRow(row)
    setDiagReturnFocus(opener)
  }

  const [historyOpen, setHistoryOpen] = useState(
    urlState.q !== undefined ||
      urlState.state !== undefined ||
      urlState.school !== undefined ||
      urlState.cursor !== undefined,
  )
  const [exportBusy, setExportBusy] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)
  const [exportNote, setExportNote] = useState<string | null>(null)

  const downloadCsv = async () => {
    setExportBusy(true)
    setExportError(null)
    setExportNote(null)
    try {
      const { blob, truncated, filename } = await exportTasksCsv({
        day,
        state: urlState.state,
        school_id: urlState.school,
        q: urlState.q,
      })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      anchor.click()
      URL.revokeObjectURL(url)
      setExportNote(truncated ? '已导出前 10000 行（超出部分被截断）。' : null)
    } catch (err) {
      setExportError(describeApiError(err))
    } finally {
      setExportBusy(false)
    }
  }

  return (
    <>
      {schoolsQuery.isError ? (
        <p className="run-summary-error" role="status">
          学校列表读取失败，下拉可能不全。
          <Button variant="outline-primary" size="sm" onClick={() => void schoolsQuery.refetch()}>
            重试
          </Button>
        </p>
      ) : null}
      {summaryQuery.isError ? (
        <p className="run-summary-error" role="status">
          摘要读取失败，计数可能缺失。
          <Button variant="outline-primary" size="sm" onClick={() => void summaryQuery.refetch()}>
            重试
          </Button>
        </p>
      ) : null}
      <RunSummary counts={summary} />

      <Card
        eyebrow="Run · 泳道"
        title={`任务时间线 · ${day}`}
        action={<span className="app-caption">{RUN_LIVE_STATUS_LABELS[liveStatus]}</span>}
      >
        <AsyncState
          status={listStatus}
          error={error ?? undefined}
          emptyLabel={
            <>
              <div>这一天没有匹配的任务</div>
              <div className="app-muted">调整业务日或筛选条件后重试。</div>
            </>
          }
          onRetry={
            tasksQuery.isError
              ? () => {
                  void tasksQuery.refetch()
                }
              : undefined
          }
        >
          <SchoolLanes lanes={lanes} onOpenDiagnostics={openDiagnostics} />
        </AsyncState>
      </Card>

      <Card
        eyebrow="Run · 检索"
        title="历史检索"
        action={
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen((value) => !value)}
          >
            {historyOpen ? '收起' : '展开'}
          </Button>
        }
      >
        {historyOpen ? (
          <>
            <RunFilters
              values={urlState}
              qInput={qInput}
              exportBusy={exportBusy}
              schools={schoolsQuery.data ?? []}
              schoolsLoading={schoolsQuery.isPending}
              onDayChange={(value) => onSearchChange(withRunFilter(urlState, { day: value }))}
              onStateChange={(value) => onSearchChange(withRunFilter(urlState, { state: value }))}
              onSchoolChange={(value) => onSearchChange(withRunFilter(urlState, { school: value }))}
              onQInputChange={setQInput}
              onQCommit={commitQuery}
              onExport={() => {
                void downloadCsv()
              }}
            />
            {exportError ? (
              <p className="run-export-error" role="alert">
                导出失败：{exportError}
              </p>
            ) : null}
            {exportNote ? <p className="app-caption">{exportNote}</p> : null}
            <AsyncState
              status={listStatus}
              error={error ?? undefined}
              emptyLabel={
                <>
                  <div>没有匹配的任务</div>
                  <div className="app-muted">调整筛选条件后重试。</div>
                </>
              }
              onRetry={
                tasksQuery.isError
                  ? () => {
                      void tasksQuery.refetch()
                    }
                  : undefined
              }
            >
              <div className="table-scroll">
                <table className="table run-table">
                  <thead>
                    <tr>
                      <th scope="col">时间</th>
                      <th scope="col">状态</th>
                      <th scope="col">账号</th>
                      <th scope="col">学校</th>
                      <th scope="col">任务</th>
                      <th scope="col">结果</th>
                      <th scope="col">
                        <span className="app-visually-hidden">操作</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.id}>
                        <td>
                          <time dateTime={row.run_at}>{formatClock(row.run_at)}</time>
                        </td>
                        <td>
                          <Badge tone={runStateTone(row.state)}>{runStateLabel(row.state)}</Badge>
                        </td>
                        <td>{row.account_phone_masked}</td>
                        <td>{row.school_name}</td>
                        <td>{row.task_name}</td>
                        <td className="run-table-result">{row.result || '—'}</td>
                        <td>
                          <Button
                            variant="outline-primary"
                            size="sm"
                            onClick={(event) => openDiagnostics(row, event.currentTarget)}
                          >
                            诊断
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="run-pager">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!hasPrevious}
                  onClick={() => {
                    const previous = goPrevious(stack)
                    setStack(previous)
                    const prev = currentCursor(previous)
                    onSearchChange(
                      prev
                        ? { ...urlState, cursor: prev }
                        : withRunFilter(urlState, {}),
                    )
                  }}
                >
                  上一页
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!tasksQuery.data?.next_cursor || tasksQuery.isFetching}
                  onClick={() => {
                    const next = tasksQuery.data?.next_cursor
                    if (!next) return
                    setStack((previous) => goNext(previous, next))
                    onSearchChange({ ...urlState, cursor: next })
                  }}
                >
                  下一页
                </Button>
              </div>
            </AsyncState>
          </>
        ) : (
          <p className="app-muted">
            展开后可按业务日、状态、学校筛选，或按学校/任务名子串、
            11 位完整手机号检索，并导出 CSV。
          </p>
        )}
      </Card>

      <RunDiagnosticsDrawer
        key={diagRow?.id ?? 'none'}
        row={diagRow}
        returnFocusTo={diagReturnFocus}
        onClose={() => setDiagRow(null)}
      />
    </>
  )
}
