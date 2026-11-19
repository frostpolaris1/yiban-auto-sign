/**
 * **功能**
 * 运行巡检页签：四指标、进程时刻、在飞清单（租约倒计时）、待重试与最近事件流。
 *
 * **归属**
 * `features/logs` 页签组件：由 `LogsView` 在 `tab=patrol` 时挂载。
 *
 * **口径**
 * 快照是单进程读数：无分页、无筛选，错误给 alert 与重试，
 * 空列表给各自的空态文案，失败不得渲染成「暂无」。
 * 待重试行以 sign_tasks 行 ID 为行身份（同账号同日可有多条），
 * 超过 200 条服务端截断并以 retrying_truncated 显式提示。
 * 租约剩余按客户端时钟每秒递减：服务端只给绝对到期时间。
 * 最近事件由容器层 `LogsView` 维护（切页签不丢），容量 20 条，
 * 最新在前；heartbeat 不列，行内带 lastEventId 证明续传位置。
 * 断线期间仅在本页签激活时每 10 秒轮询一次 `['engine-page']`。
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AsyncState } from '@/components/ui/AsyncState'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { PauseBanner } from '@/components/ui/PauseBanner'
import { ReauthDialog } from '@/components/ui/ReauthDialog'
import { describeApiError, describeReauthError } from '@/lib/api/client'
import { getEngineSnapshot, setEnginePause, type EngineSnapshot } from '@/lib/api/engine'
import type { Role } from '@/lib/api/auth'
import { formatClock, formatDateTime } from '@/lib/format'
import { SHOWCASE } from '@/lib/session'
import { showcaseEngineSnapshot } from '@/lib/showcase'
import { leaseRemainingText, useNow } from './engineDisplay'
import {
  RUN_LIVE_STATUS_LABELS,
  RUN_POLL_INTERVAL_MS,
  type RunLiveStatus,
} from './useRunLive'

/** 时钟推进间隔：租约剩余按秒展示。 */
const NOW_TICK_MS = 1000

export type EngineEvent = { key: number; type: string; at: Date; id?: string }

const EVENT_TYPE_LABELS: Record<string, string> = {
  'task.progress': '任务重排',
  'task.concluded': '任务终态',
  'window.changed': '窗口重算',
  'capacity.changed': '容量缺口',
  'egress.health': '出口健康',
  resync: '缺口重同步',
  snapshot: '前景快照',
  end: '流结束',
}

export type PatrolTabProps = {
  allowed: boolean
  role: Role
  liveStatus: RunLiveStatus
  events: EngineEvent[]
}

export function PatrolTab({ allowed, role, liveStatus, events }: PatrolTabProps) {
  const queryClient = useQueryClient()

  const engineQuery = useQuery({
    queryKey: ['engine-page', 'snapshot'],
    staleTime: 0,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseEngineSnapshot())
      return getEngineSnapshot()
    },
    enabled: allowed,
  })

  // 全局暂停开关：暂停 master+admin 可发，恢复仅 master；二次口令走 ReauthDialog。
  const [pauseDialog, setPauseDialog] = useState<'pause' | 'resume' | null>(null)
  const [pauseError, setPauseError] = useState<string | null>(null)
  const paused = engineQuery.data?.paused === true
  const pauseMutation = useMutation({
    mutationFn: (vars: { paused: boolean; password: string }) =>
      setEnginePause(vars.paused, vars.password),
    onSuccess: () => {
      setPauseDialog(null)
      setPauseError(null)
      void queryClient.invalidateQueries({ queryKey: ['engine-page'] })
      void queryClient.invalidateQueries({ queryKey: ['dashboard-engine'] })
    },
    onError: (error) => setPauseError(describeReauthError(error)),
  })
  const pauseBlockedReason =
    engineQuery.data === undefined
      ? '引擎状态读取中'
      : SHOWCASE
        ? '展示模式只读'
        : paused && role !== 'master'
          ? '仅主管理员可恢复'
          : undefined
  const pauseCanToggle = pauseBlockedReason === undefined

  // 断线降级：仅在本页签激活期间，快照每 10 秒轮询一次。连接正常时刷新只由 SSE 驱动。
  useEffect(() => {
    if (!allowed || SHOWCASE || liveStatus !== 'disconnected') return
    const timer = window.setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: ['engine-page'] })
    }, RUN_POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [allowed, liveStatus, queryClient])

  const now = useNow(NOW_TICK_MS)
  const snap = engineQuery.data ?? null
  const error = engineQuery.isError ? describeApiError(engineQuery.error) : null

  return (
    <AsyncState
      status={engineQuery.isPending ? 'loading' : engineQuery.isError ? 'error' : 'data'}
      error={error ?? undefined}
      onRetry={
        engineQuery.isError
          ? () => {
              void engineQuery.refetch()
            }
          : undefined
      }
    >
      {snap ? (
        <EngineBody
          snapshot={snap}
          now={now}
          live={liveStatus}
          events={events}
          paused={paused}
          pauseCanToggle={pauseCanToggle}
          pauseBlockedReason={pauseBlockedReason}
          onTogglePause={() => {
            setPauseError(null)
            setPauseDialog(paused ? 'resume' : 'pause')
          }}
        />
      ) : null}
      <ReauthDialog
        open={pauseDialog !== null}
        title={pauseDialog === 'resume' ? '恢复执行引擎' : '暂停执行引擎'}
        description={
          pauseDialog === 'resume'
            ? '恢复后待执行任务按序补跑。'
            : '暂停后执行器不再领取新任务，在飞任务会跑完；恢复仅主管理员，暂停后由巡检页头横幅恢复。'
        }
        confirmText={pauseDialog === 'resume' ? '恢复引擎' : '暂停引擎'}
        pending={pauseMutation.isPending}
        error={pauseError}
        onClose={() => {
          if (pauseMutation.isPending) return
          setPauseDialog(null)
          setPauseError(null)
        }}
        onConfirm={(password) =>
          pauseMutation.mutate({ paused: pauseDialog === 'pause', password })
        }
      >
        <ul className="app-pause-impact">
          <li>在飞 {engineQuery.data?.inflight.length ?? 0} 条（本管辖范围）继续完成，不会中断。</li>
          <li>待执行 {engineQuery.data?.due_now ?? 0} 条（本管辖范围）暂停领取，恢复后按序补跑。</li>
          <li>暂停是全局开关，影响所有管辖范围；恢复仅主管理员。</li>
          <li>开关写入设置并记一条审计，重启进程不丢失。</li>
        </ul>
      </ReauthDialog>
    </AsyncState>
  )
}

type EngineBodyProps = {
  snapshot: EngineSnapshot
  now: number
  live: RunLiveStatus
  events: EngineEvent[]
  paused: boolean
  pauseCanToggle: boolean
  pauseBlockedReason?: string
  onTogglePause: () => void
}

/** 快照正文：四指标、在飞、待重试与最近事件。只在有数据时渲染。 */
function EngineBody({
  snapshot,
  now,
  live,
  events,
  paused,
  pauseCanToggle,
  pauseBlockedReason,
  onTogglePause,
}: EngineBodyProps) {
  const metrics = [
    { key: 'due', label: '待执行', value: snapshot.due_now },
    { key: 'inflight', label: '在飞', value: snapshot.inflight.length },
    { key: 'success', label: '今日成功', value: snapshot.today_success },
    { key: 'failed', label: '今日失败', value: snapshot.today_failed },
  ]
  return (
    <>
      {paused ? (
        <PauseBanner
          inflight={snapshot.inflight.length}
          dueNow={snapshot.due_now}
          canResume={pauseCanToggle}
          resumeBlockedReason={pauseBlockedReason}
          onResume={onTogglePause}
        />
      ) : null}
      <Card
        eyebrow="Engine · 总览"
        title="执行概览"
        action={
          paused ? undefined : (
            <Button
              variant="ghost"
              size="sm"
              className="app-engine-pause"
              disabled={!pauseCanToggle}
              title={pauseCanToggle ? undefined : pauseBlockedReason}
              onClick={onTogglePause}
            >
              暂停引擎
            </Button>
          )
        }
      >
        <dl className="run-summary engine-summary">
          {metrics.map((cell) => (
            <div className="run-summary-cell" key={cell.key}>
              <dt>{cell.label}</dt>
              <dd>
                <span className="engine-count">{cell.value}</span>
              </dd>
            </div>
          ))}
        </dl>
        <EngineProcessTimes snapshot={snapshot} now={now} />
      </Card>

      <Card
        eyebrow="Engine · 在飞"
        title="在飞清单"
        action={<span className="app-caption">{RUN_LIVE_STATUS_LABELS[live]}</span>}
      >
        {snapshot.inflight.length === 0 ? (
          <p className="engine-empty" role="status">
            当前没有在飞任务。
            <span className="app-muted">执行循环空闲，或有任务在等待下一个落点。</span>
          </p>
        ) : (
          <div className="table-scroll">
            <table className="table run-table engine-table engine-inflight-table">
              <thead>
                <tr>
                  <th scope="col">账号</th>
                  <th scope="col">学校</th>
                  <th scope="col">认领时刻</th>
                  <th scope="col">租约剩余</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.inflight.map((row) => (
                  <tr key={row.account_id}>
                    <td>{row.account_phone_masked}</td>
                    <td>{row.school_name}</td>
                    <td>
                      <time dateTime={row.claimed_at}>{formatClock(row.claimed_at)}</time>
                    </td>
                    <td>
                      <span className="engine-lease">
                        {leaseRemainingText(new Date(row.lease_expires_at).getTime(), now)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card eyebrow="Engine · 重试" title="待重试">
        {snapshot.retrying.length === 0 ? (
          <p className="engine-empty" role="status">
            没有待重试的任务。
            <span className="app-muted">失败且当日仍有预算的任务会出现在这里。</span>
          </p>
        ) : (
          <>
            {snapshot.retrying_truncated ? (
              <p className="engine-truncated" role="status">
                待重试超过 200 条，仅显示最先到期的 200 条。
              </p>
            ) : null}
            <div className="table-scroll">
              <table className="table run-table engine-table">
                <thead>
                  <tr>
                    <th scope="col">账号</th>
                    <th scope="col">学校</th>
                    <th scope="col">任务</th>
                    <th scope="col">已试</th>
                    <th scope="col">下次执行</th>
                    <th scope="col">结果</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.retrying.map((row) => (
                    <tr key={row.task_id}>
                      <td>{row.account_phone_masked}</td>
                      <td>{row.school_name}</td>
                      <td>{row.task_name}</td>
                      <td>{row.attempts} 次</td>
                      <td>
                        <time dateTime={row.run_at}>{formatDateTime(row.run_at)}</time>
                      </td>
                      <td className="run-table-result">{row.result || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>

      <Card eyebrow="Engine · 事件" title="最近事件">
        {events.length === 0 ? (
          <p className="engine-empty" role="status">
            还没有收到业务事件。
            <span className="app-muted">任务重排、落终态与窗口重算会实时出现在这里。</span>
          </p>
        ) : (
          <ul className="engine-events">
            {events
              .slice()
              .reverse()
              .map((event) => (
                <li key={event.key}>
                  <span className="engine-event-type">
                    {EVENT_TYPE_LABELS[event.type] ?? event.type}
                    {event.id ? <span className="engine-event-id"> #{event.id}</span> : null}
                  </span>
                  <time dateTime={event.at.toISOString()}>{formatClock(event.at.toISOString())}</time>
                </li>
              ))}
          </ul>
        )}
      </Card>
    </>
  )
}

/** 进程时刻行：循环启动、最近一轮、最近认领。未启动如实话术。 */
function EngineProcessTimes({ snapshot, now }: { snapshot: EngineSnapshot; now: number }) {
  const started = snapshot.worker_started_at
  if (!started) {
    return <p className="app-caption engine-process">执行循环未在本进程启动。</p>
  }
  const parts = [`循环启动 ${formatClock(started)}`]
  if (snapshot.last_tick_at) {
    parts.push(`最近一轮 ${relativeAgo(snapshot.last_tick_at, now)}`)
  }
  if (snapshot.last_claim_at) {
    parts.push(`最近认领 ${relativeAgo(snapshot.last_claim_at, now)}`)
  }
  return <p className="app-caption engine-process">{parts.join(' · ')}</p>
}

/** 相对时刻文本：x 秒前 / x 分前，超过 1 小时给绝对时刻。 */
function relativeAgo(value: string, now: number): string {
  const at = new Date(value).getTime()
  const seconds = Math.max(0, Math.floor((now - at) / 1000))
  if (seconds < 60) return `${seconds} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分前`
  return formatClock(value)
}
