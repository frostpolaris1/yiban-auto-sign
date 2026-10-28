/**
 * **功能**
 * 时间片页装配：角色准入、日期、窗口一览、计划预览、落库与覆盖。
 *
 * **归属**
 * `features/schedule` 域容器：页面只引用它。
 *
 * **口径**
 * 角色来自 `GET /api/auth/me`。无 view 权限时不发任何 schedule 请求，
 * 只渲染无权限卡片；导航项由 `nav.ts` 的角色元数据隐藏。
 * 本页动作没有页面级置灰：能进页面的角色都能落库与覆盖。
 * 日期写入查询串；缺省取上海日历的今天（应用统一按 Asia/Shanghai 渲染）。
 * 「重新发现」只对这一次请求追加 refresh=true，不改变后续默认读缓存。
 * 预览只读；落库与覆盖都要先过确认弹窗，确认前不发写请求。
 * 写操作成功的反馈走 toast；错误反馈留在确认层内，可回看。
 * Esc 只关最上层弹窗，关哪层由本页状态决定，不依赖 Radix 的层栈。
 */

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { ConfirmModal } from '@/components/ui/ConfirmModal'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import { notifySuccess } from '@/components/ui/toast'
import { TextInput } from '@/components/ui/Input'
import type { Role } from '@/lib/api/auth'
import {
  buildSchedulePlan,
  listScheduleWindows,
  overrideScheduleWindow,
  previewSchedule,
  type ScheduleOverrideRequest,
  type SchedulePlanRequest,
  type ScheduleWindow,
} from '@/lib/api/schedule'
import { dayOf } from '@/lib/format'
import { SCHEDULE_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { SHOWCASE, useMe } from '@/lib/session'
import { showcasePlan, showcaseWindows } from '@/lib/showcase'
import { buildPlanBody, scheduleErrorText } from './drafts'
import { ScheduleOverrideModal } from './ScheduleOverrideModal'
import { SchedulePreviewModal } from './SchedulePreviewModal'
import { ScheduleWindowsTable } from './ScheduleWindowsTable'

const DENY_REASON = '你的角色无权查看时间片管理'

type ConfirmState =
  | { kind: 'build'; body: SchedulePlanRequest }
  | { kind: 'override'; body: ScheduleOverrideRequest }

type ScheduleViewProps = {
  /** 查询串里的业务日；缺省用上海日历的今天。 */
  day: string | undefined
  onDayChange: (day: string) => void
}

export function ScheduleView({ day, onDayChange }: ScheduleViewProps) {
  const queryClient = useQueryClient()
  const meQuery = useMe()
  const role: Role | null = meQuery.data?.role ?? null
  const allowed = roleAllowed(role, SCHEDULE_VIEW_ROLES)
  const activeDay = day ?? dayOf(new Date())

  /** 只对下一次取数生效的「重新发现」。取完即复位。 */
  const refreshRef = useRef(false)
  const windowsQuery = useQuery({
    queryKey: ['schedule-windows', activeDay],
    // 列表随平台发现与写操作变化。缓存键回到旧日期时必须重新取数。
    staleTime: 0,
    enabled: allowed,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseWindows(activeDay))
      const refresh = refreshRef.current
      refreshRef.current = false
      return listScheduleWindows(refresh ? { day: activeDay, refresh: true } : { day: activeDay })
    },
  })

  const [previewTarget, setPreviewTarget] = useState<ScheduleWindow | null>(null)
  const [previewReturn, setPreviewReturn] = useState<HTMLElement | null>(null)
  const [overrideOpen, setOverrideOpen] = useState(false)
  const [overrideReturn, setOverrideReturn] = useState<HTMLElement | null>(null)
  const [confirm, setConfirm] = useState<ConfirmState | null>(null)
  const [confirmReturn, setConfirmReturn] = useState<HTMLElement | null>(null)
  const [confirmError, setConfirmError] = useState<string | null>(null)

  const previewKey = [
    'schedule-preview',
    activeDay,
    previewTarget?.school_id ?? '',
    previewTarget?.task_id ?? '',
  ] as const

  const previewQuery = useQuery({
    queryKey: previewKey,
    enabled: allowed && previewTarget !== null,
    staleTime: 0,
    retry: false,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcasePlan(activeDay))
      return previewSchedule({
        day: activeDay,
        schoolId: previewTarget?.school_id ?? '',
        taskId: previewTarget?.task_id ?? '',
      })
    },
  })

  const buildMutation = useMutation({
    mutationFn: buildSchedulePlan,
    onSuccess: (plan) => {
      setConfirm(null)
      setConfirmError(null)
      setOverrideOpen(false)
      // 只关确认层；预览模态保留（焦点链不断）。落库返回的计划与覆盖同形
      // 写进预览缓存：预览显示落库后的版本与来源，不再滞留写库前状态。
      queryClient.setQueryData(previewKey, plan)
      void queryClient.invalidateQueries({ queryKey: ['schedule-windows'] })
      notifySuccess(`已落库 ${activeDay} 的计划`)
    },
    onError: (error) => setConfirmError(scheduleErrorText(error)),
  })

  const overrideMutation = useMutation({
    mutationFn: overrideScheduleWindow,
    onSuccess: (plan) => {
      // 覆盖返回重建后的计划：直接放进预览缓存，界面显示新版本与新窗口。
      queryClient.setQueryData(previewKey, plan)
      setConfirm(null)
      setConfirmError(null)
      setOverrideOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['schedule-windows'] })
      notifySuccess(`已写入覆盖，计划已重建（v${plan.version}）`)
    },
    onError: (error) => setConfirmError(scheduleErrorText(error)),
  })

  const openPreview = (row: ScheduleWindow, opener: HTMLElement) => {
    setPreviewReturn(opener)
    setPreviewTarget(row)
  }

  /**
   * 弹窗自持 Esc：按本页叠层次序只关最上层。
   * window 捕获先于 Radix 的 document 监听，且关掉的层由本页状态决定，
   * 不依赖 Radix 层栈的注册时序。
   */
  useEffect(() => {
    if (previewTarget === null) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      if (confirm !== null) {
        setConfirm(null)
        setConfirmError(null)
        return
      }
      if (overrideOpen) {
        setOverrideOpen(false)
        return
      }
      setPreviewTarget(null)
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => {
      window.removeEventListener('keydown', onKeyDown, true)
    }
  }, [previewTarget, overrideOpen, confirm])

  const closePreview = () => {
    setPreviewTarget(null)
    setOverrideOpen(false)
    setConfirm(null)
    setConfirmError(null)
  }

  const openOverride = (opener: HTMLElement) => {
    setOverrideReturn(opener)
    setOverrideOpen(true)
  }

  const openBuildConfirm = (opener: HTMLElement) => {
    if (!previewTarget) return
    setConfirmError(null)
    setConfirmReturn(opener)
    setConfirm({
      kind: 'build',
      body: buildPlanBody({
        day: activeDay,
        schoolId: previewTarget.school_id,
        taskId: previewTarget.task_id,
      }),
    })
  }

  const openOverrideConfirm = (body: ScheduleOverrideRequest, opener: HTMLElement) => {
    setConfirmError(null)
    setConfirmReturn(opener)
    setConfirm({ kind: 'override', body })
  }

  const rows = windowsQuery.data?.items ?? []
  const listError = windowsQuery.isError ? scheduleErrorText(windowsQuery.error) : null
  const previewError = previewQuery.isError ? scheduleErrorText(previewQuery.error) : null

  return (
    <>
      <PageHeader
        eyebrow="工作区 · 时间片"
        title={
          <>
            签到 <span className="accent">窗口</span>
          </>
        }
        subtitle="按日查看任务窗口。预览只读；落库与覆盖会写库，覆盖会立即重排 pending 行。"
      />

      {!allowed ? (
        <DeniedCard eyebrow="Schedule" reason={DENY_REASON} />
      ) : (
        <Card
          eyebrow="Schedule"
          title="窗口一览"
          action={<span className="app-caption">缺省为服务器时区的今天</span>}
        >
          <div className="app-schedule-toolbar">
            <TextInput
              id="schedule-day"
              label="日期"
              type="date"
              value={activeDay}
              onChange={(event) => {
                if (event.target.value !== '') onDayChange(event.target.value)
              }}
            />
            <Button
              variant="secondary"
              aria-label="重新发现"
              disabled={windowsQuery.isFetching}
              onClick={() => {
                refreshRef.current = true
                void windowsQuery.refetch()
              }}
            >
              {windowsQuery.isFetching ? '正在发现…' : '重新发现'}
            </Button>
          </div>

          <ScheduleWindowsTable
            rows={rows}
            loading={windowsQuery.isLoading}
            error={listError}
            onRetry={() => {
              void windowsQuery.refetch()
            }}
            onPreview={openPreview}
          />
        </Card>
      )}

      <SchedulePreviewModal
        target={previewTarget}
        day={activeDay}
        plan={previewQuery.data}
        pending={previewQuery.isPending || previewQuery.isFetching}
        error={previewError}
        returnFocusTo={previewReturn}
        onClose={closePreview}
        onRetry={() => {
          void previewQuery.refetch()
        }}
        onOverride={openOverride}
        onBuild={openBuildConfirm}
      />

      <ScheduleOverrideModal
        key={`${previewTarget?.school_id ?? ''}:${previewTarget?.task_id ?? ''}:${previewQuery.data?.version ?? 0}`}
        open={overrideOpen && previewQuery.data !== undefined}
        target={previewTarget}
        plan={previewQuery.data}
        returnFocusTo={overrideReturn}
        onClose={() => setOverrideOpen(false)}
        onNext={openOverrideConfirm}
      />

      <ConfirmModal
        open={confirm !== null}
        // 落库确认直接叠在预览上（第 2 层）；覆盖确认还要再叠一层（第 3 层）。
        layer={confirm?.kind === 'override' ? 3 : 2}
        title={confirm?.kind === 'override' ? '写入覆盖？' : '落库此计划？'}
        description={
          confirm?.kind === 'override'
            ? '覆盖会立即生效：pending 行将立即重排；running 与终态行不受影响。'
            : `将把 ${activeDay} 的计划写入库。窗口变更时 pending 行会立即重排；running 与终态行不受影响。`
        }
        confirmText={confirm?.kind === 'override' ? '确认覆盖' : '确认落库'}
        pending={buildMutation.isPending || overrideMutation.isPending}
        error={confirmError}
        returnFocusTo={confirmReturn}
        onClose={() => {
          setConfirm(null)
          setConfirmError(null)
        }}
        onConfirm={() => {
          if (confirm === null) return
          setConfirmError(null)
          if (confirm.kind === 'build') buildMutation.mutate(confirm.body)
          else overrideMutation.mutate(confirm.body)
        }}
      />
    </>
  )
}
