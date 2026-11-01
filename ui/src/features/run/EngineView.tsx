/**
 * **功能**
 * 执行监控装配：四指标、在飞清单、待重试与最近事件，一段一卡。
 *
 * **归属**
 * `features/run` 域容器：页面只引用它。与运行页共用
 * `useRunLive` 的连接纪律与 `AsyncState` 的状态呈现。
 *
 * **口径**
 * 角色来自 `GET /api/auth/me`。无 view 权限时不发快照请求，
 * 只渲染无权限卡片；导航项由 `nav.ts` 的角色元数据隐藏。
 * 快照是单进程读数：无分页、无筛选，错误给 alert 与重试，
 * 空列表给各自的空态文案，失败不得渲染成「暂无」。
 * 租约剩余按客户端时钟每秒递减：服务端只给绝对到期时间。
 * 最近事件是本页开机以来经 SSE 收到的业务事件，容量 20 条，
 * 新事件在后；断线出横幅并可重连，断线期间快照每 10 秒轮询。
 * SSE 到达失效 `['engine-page']` 前缀查询；学校下拉不参与。
 * 展示构建（`VITE_SHOWCASE=1`）取合成夹具，零网络请求。
 */

import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AsyncState } from '@/components/ui/AsyncState'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import { describeApiError } from '@/lib/api/client'
import { getEngineSnapshot, type EngineSnapshot } from '@/lib/api/engine'
import { formatClock, formatDateTime } from '@/lib/format'
import { ENGINE_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { SHOWCASE, useMe } from '@/lib/session'
import { showcaseEngineSnapshot } from '@/lib/showcase'
import { leaseRemainingText, useNow } from './engineDisplay'
import { RUN_POLL_INTERVAL_MS, useRunLive } from './useRunLive'

const DENY_REASON = '你的角色无权查看执行监控'

/** 最近事件列表的容量。 */
const EVENT_LIMIT = 20

/** 时钟推进间隔：租约剩余按秒展示。 */
const NOW_TICK_MS = 1000

type EngineEvent = { key: number; type: string; at: Date }

const EVENT_TYPE_LABELS: Record<string, string> = {
  'task.progress': '任务重排',
  'task.concluded': '任务终态',
  'window.changed': '窗口重算',
  'capacity.changed': '容量缺口',
  'egress.health': '出口健康',
}

export function EngineView() {
  const queryClient = useQueryClient()
  const meQuery = useMe()
  const role = meQuery.data?.role ?? null
  const allowed = roleAllowed(role, ENGINE_VIEW_ROLES)

  const [events, setEvents] = useState<EngineEvent[]>([])
  const eventKey = useRef(0)
  const onEvent = (type: string) => {
    eventKey.current += 1
    setEvents((previous) =>
      [...previous, { key: eventKey.current, type, at: new Date() }].slice(-EVENT_LIMIT),
    )
  }

  const live = useRunLive(queryClient, allowed, undefined, {
    onEvent,
    invalidateKey: ['engine-page'],
  })

  const engineQuery = useQuery({
    queryKey: ['engine-page', 'snapshot'],
    staleTime: 0,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseEngineSnapshot())
      return getEngineSnapshot()
    },
    enabled: allowed,
  })

  // 断线降级：快照每 10 秒轮询一次。连接正常时刷新只由 SSE 驱动。
  useEffect(() => {
    if (!allowed || live.status !== 'disconnected') return
    const timer = window.setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: ['engine-page'] })
    }, RUN_POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [allowed, live.status, queryClient])

  const now = useNow(NOW_TICK_MS)
  const snap = engineQuery.data ?? null
  const error = engineQuery.isError ? describeApiError(engineQuery.error) : null

  return (
    <>
      <PageHeader
        eyebrow="Engine · 执行监控"
        title={
          <>
            执行<span className="accent">监控</span>
          </>
        }
        subtitle="此刻谁在签、今天签成了多少、哪些在等重试，都在这一页。"
      />

      {live.status === 'disconnected' ? (
        <div className="run-live-banner" role="status">
          <span>已停止实时，最近更新 {live.since ?? '—'}。</span>
          <Button variant="outline-primary" size="sm" onClick={live.reconnect}>
            重连
          </Button>
        </div>
      ) : null}

      {!allowed ? (
        <DeniedCard eyebrow="Engine" reason={DENY_REASON} />
      ) : (
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
          {snap ? <EngineBody snapshot={snap} now={now} live={live.status} events={events} /> : null}
        </AsyncState>
      )}
    </>
  )
}

type EngineBodyProps = {
  snapshot: EngineSnapshot
  now: number
  live: 'live' | 'disconnected'
  events: EngineEvent[]
}

/** 快照正文：四指标、在飞、待重试与最近事件。只在有数据时渲染。 */
function EngineBody({ snapshot, now, live, events }: EngineBodyProps) {
  const metrics = [
    { key: 'due', label: '待执行', value: snapshot.due_now },
    { key: 'inflight', label: '在飞', value: snapshot.inflight.length },
    { key: 'success', label: '今日成功', value: snapshot.today_success },
    { key: 'failed', label: '今日失败', value: snapshot.today_failed },
  ]
  return (
    <>
      <Card eyebrow="Engine · 总览" title="执行概览">
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
        action={<span className="app-caption">{live === 'live' ? '实时' : '已停止实时'}</span>}
      >
        {snapshot.inflight.length === 0 ? (
          <p className="engine-empty" role="status">
            当前没有在飞任务。
            <span className="app-muted">执行循环空闲，或有任务在等待下一个落点。</span>
          </p>
        ) : (
          <table className="table run-table engine-table">
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
        )}
      </Card>

      <Card eyebrow="Engine · 重试" title="待重试">
        {snapshot.retrying.length === 0 ? (
          <p className="engine-empty" role="status">
            没有待重试的任务。
            <span className="app-muted">失败且当日仍有预算的任务会出现在这里。</span>
          </p>
        ) : (
          <table className="table run-table engine-table">
            <thead>
              <tr>
                <th scope="col">账号</th>
                <th scope="col">学校</th>
                <th scope="col">已试</th>
                <th scope="col">下次执行</th>
                <th scope="col">结果</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.retrying.map((row) => (
                <tr key={row.account_id}>
                  <td>{row.account_phone_masked}</td>
                  <td>{row.school_name}</td>
                  <td>{row.attempts} 次</td>
                  <td>
                    <time dateTime={row.run_at}>{formatDateTime(row.run_at)}</time>
                  </td>
                  <td className="run-table-result">{row.result || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
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
                  <span className="engine-event-type">{EVENT_TYPE_LABELS[event.type] ?? event.type}</span>
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
