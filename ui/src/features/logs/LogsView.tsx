/**
 * **功能**
 * 签到台账容器（`/logs`）：合并「签到事件」与「运行巡检」两页签，共用单条 SSE 实时流。
 *
 * **归属**
 * `features/logs` 域容器：页面 `LogsPage` 只引用它。
 *
 * **口径**
 * 角色来自 `GET /api/auth/me`。无 `RUN_VIEW_ROLES` 权限时不发任务或快照请求，
 * 只渲染无权限卡片；导航项由 `nav.ts` 的角色元数据隐藏。
 * 页签状态进 URL：`/logs?tab=events|patrol`，默认 `events`。
 * 两页签共用一条 SSE 连接（`useRunLive` 在容器层常驻），切页签不重建连接；
 * 最近事件列表（容量 20 条，heartbeat 不列）在容器层累积，传给巡检页签。
 * 断线 10s 轮询由当前激活页签各自驱动，不跨页签空刷。
 */

import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import { Tabs } from '@/components/ui/Tabs'
import { RUN_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { SHOWCASE, useMe } from '@/lib/session'
import { EventsTab } from './EventsTab'
import { PatrolTab, type EngineEvent } from './PatrolTab'
import { RunLiveBanner } from './RunLiveBanner'
import { useRunLive } from './useRunLive'
import type { LogsTab, RunURLState } from './urlState'

const DENY_REASON = '你的角色无权查看签到台账'

/** 最近事件列表的容量。 */
const EVENT_LIMIT = 20

export type LogsViewProps = {
  urlState: RunURLState
  onSearchChange: (next: RunURLState, opts?: { replace?: boolean }) => void
}

export function LogsView({ urlState, onSearchChange }: LogsViewProps) {
  const queryClient = useQueryClient()
  const meQuery = useMe()
  const role = meQuery.data?.role ?? null
  const allowed = roleAllowed(role, RUN_VIEW_ROLES)

  const [events, setEvents] = useState<EngineEvent[]>([])
  const eventKey = useRef(0)
  const onEvent = (type: string, id?: string) => {
    eventKey.current += 1
    setEvents((previous) =>
      [...previous, { key: eventKey.current, type, at: new Date(), id }].slice(-EVENT_LIMIT),
    )
  }

  const live = useRunLive(queryClient, allowed && !SHOWCASE, undefined, {
    onEvent,
    onInvalidate: () => {
      void queryClient.invalidateQueries({ queryKey: ['run-page'] })
      void queryClient.invalidateQueries({ queryKey: ['engine-page'] })
    },
  })

  const activeTab: LogsTab = urlState.tab ?? 'events'

  return (
    <div className="logs-page">
      <PageHeader
        eyebrow="今日 · 台账"
        title={
          <>
            签到 <span className="accent">台账</span>
          </>
        }
        subtitle="今日签到事件、学校泳道与单进程运行巡检，一页统览。"
      />

      <RunLiveBanner live={live} />

      {!allowed ? (
        <DeniedCard eyebrow="今日 · 台账" reason={DENY_REASON} />
      ) : (
        <Tabs
          label="签到台账视图"
          value={activeTab}
          onValueChange={(next) => {
            onSearchChange({ ...urlState, tab: next as LogsTab })
          }}
          items={[
            {
              value: 'events',
              label: '签到事件',
              content: (
                <EventsTab
                  urlState={urlState}
                  onSearchChange={onSearchChange}
                  allowed={allowed}
                  liveStatus={live.status}
                />
              ),
            },
            {
              value: 'patrol',
              label: '运行巡检',
              content: (
                  <PatrolTab
                    allowed={allowed}
                    role={role ?? 'user'}
                    liveStatus={live.status}
                    events={events}
                  />
                ),
            },
          ]}
        />
      )}
    </div>
  )
}
