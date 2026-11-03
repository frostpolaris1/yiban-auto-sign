/**
 * **功能**
 * 看板装配：按角色取数，渲染 KPI 与对应卡片。
 *
 * **归属**
 * `features/dashboard` 域容器：页面只引用它。
 *
 * **口径**
 * 角色取数隔离是本容器的第一不变式：
 * user 只读 sign-status 与 diagnostics；master/admin 只读 windows、capacity 与 sign-stats。
 * 未命中的查询一律 `enabled: false`，挂载即零请求。
 * sign-status 每 30 秒轮询；页面隐藏时不轮询（`refetchIntervalInBackground: false`）。
 * windows 与时间片页共用缓存键 `['schedule-windows', day]`。
 * master/admin 的 KPI 取 `sign-stats` 报告末两天的今日终态，与图表共用一次读取；
 * 缓存键带上海业务日，每 60 秒复核、跨日才重取。
 * 容量默认选第一个可用任务；没有可用任务就不发容量请求。
 * 各卡错误态独立，重试只重取该卡的数据。
 * 展示模式用合成夹具，零网络请求。
 */

import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { describeApiError } from '@/lib/api/client'
import { getDiagnostics, getSignStatus } from '@/lib/api/me'
import type { Role } from '@/lib/api/auth'
import { getCapacity, listScheduleWindows } from '@/lib/api/schedule'
import { getPrefStats, getSignStats } from '@/lib/api/stats'
import { dayOf } from '@/lib/format'
import { SHOWCASE } from '@/lib/session'
import { showcaseCapacity, showcasePrefStats, showcaseSignStats, showcaseWindows } from '@/lib/showcase'
import { CapacityCard } from './CapacityCard'
import { PrefCrowdingCard, SignDistributionCard, SignTrendCard } from './ChartCards'
import { DiagnosticsCard } from './DiagnosticsCard'
import { KpiGrid } from './KpiGrid'
import { SignStatusCard } from './SignStatusCard'
import {
  capacityOptions,
  signCounts,
  signKpiCards,
  todayKpiCards,
  todayStats,
  windowCounts,
} from './display'

const SIGN_STATUS_KEY = ['me-sign-status'] as const
const DIAGNOSTICS_KEY = ['me-diagnostics'] as const
const SIGN_POLL_MS = 30_000

type DashboardViewProps = {
  role: Role
  /** user 视角的附加卡（时间偏好在 `features/schedule`，由页面传入）。 */
  extraCards?: ReactNode
}

export function DashboardView({ role, extraCards }: DashboardViewProps) {
  const isUser = role === 'user'
  const day = dayOf(new Date())

  const signStatusQuery = useQuery({
    queryKey: SIGN_STATUS_KEY,
    queryFn: getSignStatus,
    enabled: isUser,
    // 轮询取最新状态：每次挂载与每次到期都要重新取数。
    staleTime: 0,
    retry: false,
    refetchInterval: SIGN_POLL_MS,
    refetchIntervalInBackground: false,
  })

  const diagnosticsQuery = useQuery({
    queryKey: DIAGNOSTICS_KEY,
    queryFn: getDiagnostics,
    enabled: isUser,
    staleTime: 30_000,
    retry: false,
  })

  const windowsQuery = useQuery({
    queryKey: ['schedule-windows', day],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseWindows(day)) : listScheduleWindows({ day }),
    enabled: !isUser,
    staleTime: 0,
    retry: false,
  })

  const options = capacityOptions(windowsQuery.data?.items ?? [])
  const [picked, setPicked] = useState('')
  const selected = options.find((option) => option.value === picked) ?? options[0] ?? null

  const capacityQuery = useQuery({
    queryKey: ['capacity', day, selected?.schoolId ?? '', selected?.taskId ?? ''],
    queryFn: () =>
      SHOWCASE
        ? Promise.resolve(
            showcaseCapacity({
              day,
              schoolId: selected?.schoolId ?? '',
              taskId: selected?.taskId ?? '',
            }),
          )
        : getCapacity({
            day,
            schoolId: selected?.schoolId ?? '',
            taskId: selected?.taskId ?? '',
          }),
    enabled: !isUser && selected !== null,
    staleTime: 0,
    retry: false,
  })

  // 聚合只随页面挂载、业务日跨日与手动重试取数：30 天窗口不参与 30 秒轮询。
  // 业务日按上海时区逐分钟复核（dayOf）：跨日才换缓存键触发一次重取，
  // 长开的页面不会把昨日报告继续标成「今日」；不做聚合轮询。
  const [businessDay, setBusinessDay] = useState(() => dayOf(new Date()))
  useEffect(() => {
    const timer = window.setInterval(() => {
      setBusinessDay(dayOf(new Date()))
    }, 60_000)
    return () => window.clearInterval(timer)
  }, [])

  const signStatsQuery = useQuery({
    queryKey: ['sign-stats', 30, businessDay],
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseSignStats()) : getSignStats(30)),
    enabled: !isUser,
    staleTime: 0,
    retry: false,
  })

  const prefStatsQuery = useQuery({
    queryKey: ['pref-stats', selected?.schoolId ?? '', selected?.taskId ?? '', day],
    queryFn: () =>
      SHOWCASE
        ? Promise.resolve(
            showcasePrefStats({
              day,
              schoolId: selected?.schoolId ?? '',
              taskId: selected?.taskId ?? '',
            }),
          )
        : getPrefStats({
            day,
            schoolId: selected?.schoolId ?? '',
            taskId: selected?.taskId ?? '',
          }),
    enabled: !isUser && selected !== null,
    staleTime: 0,
    retry: false,
  })

  const signStatusItems = signStatusQuery.data?.items ?? []
  const diagnosticsItems = diagnosticsQuery.data?.items ?? []
  // master/admin 的四张 KPI 与图表共用这份 30 天报告；只读末两天作今日终态。
  const stats = todayStats(signStatsQuery.data)

  if (isUser) {
    return (
      <>
        <KpiGrid
          cards={signKpiCards(
            signStatusQuery.data ? signCounts(signStatusQuery.data.items) : null,
          )}
        />
        <div className="grid">
          <SignStatusCard
            items={signStatusItems}
            updatedAt={signStatusQuery.data?.updated_at ?? null}
            loading={signStatusQuery.isPending}
            error={
              signStatusQuery.isError ? describeApiError(signStatusQuery.error) : null
            }
            onRetry={() => {
              void signStatusQuery.refetch()
            }}
          />
          <DiagnosticsCard
            items={diagnosticsItems}
            loading={diagnosticsQuery.isPending}
            error={
              diagnosticsQuery.isError ? describeApiError(diagnosticsQuery.error) : null
            }
            onRetry={() => {
              void diagnosticsQuery.refetch()
            }}
          />
          {extraCards}
        </div>
      </>
    )
  }

  return (
    <>
      <KpiGrid cards={todayKpiCards(stats)} />
      <div className="grid">
        <CapacityCard
          options={options}
          selectedValue={selected?.value ?? ''}
          onSelect={setPicked}
          report={capacityQuery.data}
          counts={windowsQuery.data ? windowCounts(windowsQuery.data.items) : null}
          windowsLoading={windowsQuery.isPending}
          windowsError={windowsQuery.isError ? describeApiError(windowsQuery.error) : null}
          capacityLoading={capacityQuery.isPending}
          capacityError={
            capacityQuery.isError ? describeApiError(capacityQuery.error) : null
          }
          onRetryWindows={() => {
            void windowsQuery.refetch()
          }}
          onRetryCapacity={() => {
            void capacityQuery.refetch()
          }}
        />
      </div>
      <div className="grid">
        <SignTrendCard
          report={signStatsQuery.data}
          loading={signStatsQuery.isPending}
          error={signStatsQuery.isError ? describeApiError(signStatsQuery.error) : null}
          onRetry={() => {
            void signStatsQuery.refetch()
          }}
        />
        <SignDistributionCard
          report={signStatsQuery.data}
          loading={signStatsQuery.isPending}
          error={signStatsQuery.isError ? describeApiError(signStatsQuery.error) : null}
          onRetry={() => {
            void signStatsQuery.refetch()
          }}
        />
        <PrefCrowdingCard
          report={prefStatsQuery.data}
          taskLabel={selected?.label ?? null}
          loading={prefStatsQuery.isPending && selected !== null}
          error={prefStatsQuery.isError ? describeApiError(prefStatsQuery.error) : null}
          onRetry={() => {
            void prefStatsQuery.refetch()
          }}
        />
      </div>
    </>
  )
}
