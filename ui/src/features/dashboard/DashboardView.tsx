/**
 * **功能**
 * 看板装配：按角色取数，渲染 KPI、容量体检、拥挤度、30 天趋势与底部健康状态条。
 *
 * **归属**
 * `features/dashboard` 域容器：页面只引用它。
 *
 * **口径**
 * ticket13 起 user 不再进管理壳：壳层一律把 user 302 到用户端 `/app`。
 * 本容器只服务 master/admin：windows、capacity、audit-status、engine、
 * sign-stats 与 account summary；未选任务前容量与拥挤度保持 `enabled: false`，
 * 挂载即零请求。
 * audit-status 与 engine 快照在挂载时按需读取，失败只挂对应区段。
 * windows 与时间片页共用缓存键 `['schedule-windows', day]`。
 * master/admin 的 KPI 取 `sign-stats` 报告末两天的今日终态，与趋势图共用一次读取；
 * 缓存键带上海业务日，每 60 秒复核、跨日才重取。
 * 首屏顺序＝KPI×4（含待处理账号直达入口）→容量体检+时段拥挤度（并排）→30天趋势+今日失败明细→底部健康与审计状态条。
 * 失败明细走独立端点（按失败结果分桶），不重读 sign-stats：趋势与 KPI 共用那一次读取。
 * 拥挤度每 15 秒轮询（自选写入无 SSE 事件）；页面隐藏时不轮询。
 * 容量默认选第一个可用任务；没有可用任务就不发容量与拥挤度请求。
 * 各卡错误态独立，重试只重取该卡的数据。
 * 展示模式用合成夹具，零网络请求。
 */

import { useEffect, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getAccountSummary } from '@/lib/api/account'
import { describeApiError, describeReauthError } from '@/lib/api/client'
import { getAuditStatus } from '@/lib/api/audit'
import { getAnnouncement } from '@/lib/api/announcement'
import { getEngineSnapshot, setEnginePause } from '@/lib/api/engine'
import type { Role } from '@/lib/api/auth'
import { getCapacity, listScheduleWindows } from '@/lib/api/schedule'
import { getPrefStats, getSignFailures, getSignStats } from '@/lib/api/stats'
import { dayOf } from '@/lib/format'
import { SHOWCASE } from '@/lib/session'
import {
  showcaseAccountSummary,
  showcaseAnnouncement,
  showcaseAuditStatus,
  showcaseCapacity,
  showcaseEngineSnapshot,
  showcasePrefStats,
  showcaseSignFailures,
  showcaseSignStats,
  showcaseWindows,
} from '@/lib/showcase'
import { Alert } from '@/components/ui/Alert'
import { ReauthDialog } from '@/components/ui/ReauthDialog'
import { AuditStatusCard } from './AuditStatusCard'
import { CapacityCard } from './CapacityCard'
import { PrefCrowdingCard, SignTrendCard } from './ChartCards'
import { FailureBreakdownCard } from './FailureBreakdownCard'
import { KpiGrid } from './KpiGrid'
import {
  capacityOptions,
  todayKpiCards,
  todayStats,
  windowCounts,
} from './display'

/**
 * 公告关闭标记只活在本页会话的内存里：跨路由保留（组件卸载不丢），
 * 页面刷新回初值（验收口径「本会话内不再出现、刷新回」）。
 */
let announcementDismissedForSession = false

/**
 * 公告横幅：有正文且本会话未关闭时渲染；关闭状态只活在会话内，不写偏好。
 * 无公告时零 DOM；不弹窗、不打断阅读。
 */
function announcementBannerNode(
  text: string,
  dismissed: boolean,
  onDismiss: (next: boolean) => void,
) {
  if (text === '' || dismissed) return null
  return (
    <div className="app-announcement-banner">
      <Alert tone="info" title="全站公告" dismissible onDismiss={() => onDismiss(true)}>
        {text}
      </Alert>
    </div>
  )
}

type DashboardViewProps = {
  role: Role
}

export function DashboardView({ role }: DashboardViewProps) {
  const day = dayOf(new Date())

  const windowsQuery = useQuery({
    queryKey: ['schedule-windows', day],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseWindows(day)) : listScheduleWindows({ day }),
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
    enabled: selected !== null,
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
    staleTime: 0,
    retry: false,
  })

  // 今日失败明细：独立端点（按失败结果分桶），不与 30 天报告共用读取。
  // 不传 day：业务日由服务端按部署时区判定，跨日复核复用同一份 businessDay 键。
  const signFailuresQuery = useQuery({
    queryKey: ['sign-failures', businessDay],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseSignFailures()) : getSignFailures(),
    staleTime: 0,
    retry: false,
  })
  // 审计链巡检：按需复算全链，耗时随行数线性增长，故不轮询；
  // 只在挂载、重试或缓存过期后取一次。展示构建读合成结论，零请求。
  const auditStatusQuery = useQuery({
    queryKey: ['audit-status'],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseAuditStatus()) : getAuditStatus(),
    staleTime: 60_000,
    retry: false,
  })

  // 引擎运行脉冲：单次快照读取，与顶部健康条绑定；失败只影响本区段。
  const engineQuery = useQuery({
    queryKey: ['dashboard-engine'],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseEngineSnapshot()) : getEngineSnapshot(),
    staleTime: 15_000,
    retry: false,
  })

  // 全站公告横幅：读公开 GET /api/announcement；本会话内可关闭，不写偏好。
  const announcementQuery = useQuery({
    queryKey: ['announcement'],
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseAnnouncement()) : getAnnouncement()),
    staleTime: 60_000,
    retry: false,
  })
  const [announcementDismissed, setAnnouncementDismissedState] = useState(
    announcementDismissedForSession,
  )
  const setAnnouncementDismissed = (next: boolean) => {
    announcementDismissedForSession = next
    setAnnouncementDismissedState(next)
  }
  const announcementText = announcementQuery.data?.text.trim() ?? ''

  // 全局暂停开关：暂停 master+admin 可发，恢复仅 master；二次口令走 ReauthDialog。
  const queryClient = useQueryClient()
  const [pauseDialog, setPauseDialog] = useState<'pause' | 'resume' | null>(null)
  const [pauseError, setPauseError] = useState<string | null>(null)
  const [pauseReturnFocus, setPauseReturnFocus] = useState<HTMLElement | null>(null)
  const paused = engineQuery.data?.paused === true
  const pauseMutation = useMutation({
    mutationFn: (vars: { paused: boolean; password: string }) =>
      setEnginePause(vars.paused, vars.password),
    onSuccess: () => {
      setPauseDialog(null)
      setPauseError(null)
      void queryClient.invalidateQueries({ queryKey: ['dashboard-engine'] })
      void queryClient.invalidateQueries({ queryKey: ['engine-page'] })
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
  const pauseAction = {
    paused,
    canToggle: pauseBlockedReason === undefined,
    blockedReason: pauseBlockedReason,
    onToggle: (opener?: HTMLElement | null) => {
      setPauseError(null)
      setPauseReturnFocus(opener ?? null)
      setPauseDialog(paused ? 'resume' : 'pause')
    },
  }

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
    enabled: selected !== null,
    staleTime: 0,
    retry: false,
    // 拥挤度是「实时」诉求：15 秒轮询；页面隐藏时不轮询。
    // 自选写入没有 SSE 事件，轮询是唯一刷新源；展示模式轮询合成夹具零请求。
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
  })

  // 四张 KPI 与图表共用这份 30 天报告；只读末两天作今日终态。
  const stats = todayStats(signStatsQuery.data)

  // 待处理账号：与账号页提示条共用缓存键；读数失败只挂该卡（显「—」）。
  const accountSummaryQuery = useQuery({
    queryKey: ['account-summary'],
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseAccountSummary()) : getAccountSummary()),
    staleTime: 0,
    retry: false,
  })

  return (
    <>
      {announcementBannerNode(announcementText, announcementDismissed, setAnnouncementDismissed)}
      <nav className="app-quick-links" aria-label="管理常用直达">
        <span className="app-quick-links-label">常用直达：</span>
        <Link to="/queue" className="app-inline-link">
          待办处理
        </Link>
        <span className="app-quick-links-sep" aria-hidden="true">
          ·
        </span>
        <Link to="/audit" className="app-inline-link">
          审计链巡检
        </Link>
        <span className="app-quick-links-sep" aria-hidden="true">
          ·
        </span>
        <Link to="/system" className="app-inline-link">
          系统设置
        </Link>
      </nav>
      <KpiGrid
        cards={todayKpiCards(stats, accountSummaryQuery.data ?? null)}
      />
      <div className="grid app-dashboard-workbench">
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
      <div className="grid">
        <SignTrendCard
          report={signStatsQuery.data}
          loading={signStatsQuery.isPending}
          error={signStatsQuery.isError ? describeApiError(signStatsQuery.error) : null}
          onRetry={() => {
            void signStatsQuery.refetch()
          }}
        />
        <FailureBreakdownCard
          report={signFailuresQuery.data}
          loading={signFailuresQuery.isPending}
          error={
            signFailuresQuery.isError ? describeApiError(signFailuresQuery.error) : null
          }
          onRetry={() => {
            void signFailuresQuery.refetch()
          }}
        />
      </div>
      <AuditStatusCard
        report={auditStatusQuery.data}
        loading={auditStatusQuery.isPending}
        error={
          auditStatusQuery.isError ? describeApiError(auditStatusQuery.error) : null
        }
        onRetry={() => {
          void auditStatusQuery.refetch()
        }}
        engine={engineQuery.data}
        engineLoading={engineQuery.isPending}
        engineError={
          engineQuery.isError ? describeApiError(engineQuery.error) : null
        }
        onRetryEngine={() => {
          void engineQuery.refetch()
        }}
        pause={pauseAction}
      />
      <ReauthDialog
        open={pauseDialog !== null}
        title={pauseDialog === 'resume' ? '恢复执行引擎' : '暂停执行引擎'}
        description={
          pauseDialog === 'resume'
            ? '恢复后待执行任务按序补跑。'
            : '暂停后执行器不再领取新任务，在飞任务会跑完；恢复仅主管理员，暂停后由健康条横幅恢复。'
        }
        confirmText={pauseDialog === 'resume' ? '恢复引擎' : '暂停引擎'}
        pending={pauseMutation.isPending}
        error={pauseError}
        returnFocusTo={pauseReturnFocus}
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
    </>
  )
}
