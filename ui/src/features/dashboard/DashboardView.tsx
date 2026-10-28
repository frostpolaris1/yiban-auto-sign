/**
 * **功能**
 * 看板装配：按角色取数，渲染 KPI 与对应卡片。
 *
 * **归属**
 * `features/dashboard` 域容器：页面只引用它。
 *
 * **口径**
 * 角色取数隔离是本容器的第一不变式：
 * user 只读 sign-status 与 diagnostics；master/admin 只读 windows 与 capacity。
 * 未命中的查询一律 `enabled: false`，挂载即零请求。
 * sign-status 每 30 秒轮询；页面隐藏时不轮询（`refetchIntervalInBackground: false`）。
 * windows 与时间片页共用缓存键 `['schedule-windows', day]`。
 * 容量默认选第一个可用任务；没有可用任务就不发容量请求。
 * 各卡错误态独立，重试只重取该卡的数据。
 * 展示模式用合成夹具，零网络请求。
 */

import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { describeApiError } from '@/lib/api/client'
import { getDiagnostics, getSignStatus } from '@/lib/api/me'
import type { Role } from '@/lib/api/auth'
import { getCapacity, listScheduleWindows } from '@/lib/api/schedule'
import { dayOf } from '@/lib/format'
import { SHOWCASE } from '@/lib/session'
import { showcaseCapacity, showcaseWindows } from '@/lib/showcase'
import { CapacityCard } from './CapacityCard'
import { DiagnosticsCard } from './DiagnosticsCard'
import { KpiGrid } from './KpiGrid'
import { SignStatusCard } from './SignStatusCard'
import {
  capacityOptions,
  signCounts,
  signKpiCards,
  windowCounts,
  windowKpiCards,
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

  const signStatusItems = signStatusQuery.data?.items ?? []
  const diagnosticsItems = diagnosticsQuery.data?.items ?? []

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
      <KpiGrid
        cards={windowKpiCards(
          windowsQuery.data ? windowCounts(windowsQuery.data.items) : null,
        )}
      />
      <div className="grid">
        <CapacityCard
          options={options}
          selectedValue={selected?.value ?? ''}
          onSelect={setPicked}
          report={capacityQuery.data}
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
    </>
  )
}
