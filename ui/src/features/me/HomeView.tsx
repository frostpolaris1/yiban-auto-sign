/**
 * **功能**
 * 用户端首页：一句问候、公告、今日 Hero、7 天条与桌面右栏。
 *
 * **归属**
 * `features/me` 域容器：取数与状态派生在这里，展示原语在 components/ui。
 *
 * **口径**
 * 一人一号：>1 个活跃账号给异常卡，不静默取第一条。
 * 账号状态优先于今日结果；任何态都不只写「今日暂无任务」。
 * Hero 是唯一视觉主角；公告、下一次、7 天条是安静行，不再套卡。
 * 窗口轴只对 active 账号且有今日任务行绘制；没有时间片数据时只画窗口。
 * 手动签到 202 后 3 秒轮询，终态或 60 秒后回到 30 秒。
 * 错误留在区块内，可回看；成功反馈走 toast。
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AnimatedNumber } from '@/components/ui/AnimatedNumber'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Modal'
import { notifySuccess } from '@/components/ui/toast'
import { pauseAccount, signAccount } from '@/lib/api/account'
import type { Account, AccountStatus } from '@/lib/api/account'
import { ApiError } from '@/lib/api/client'
import { photoBytesPath } from '@/lib/api/photo'
import { formatClock } from '@/lib/format'
import {
  displayName,
  parseTodayState,
  userStatusLabel,
  userStatusTone,
} from '@/lib/account-display'
import {
  businessDateLabel,
  greetingFor,
  homeState,
  nextRunText,
  todayKey,
  windowGeometry,
  type HomeState,
  type WindowGeometry,
} from './display'
import { WeekStrip } from './WeekStrip'
import {
  ME_KEYS,
  useAnnouncement,
  useDiagnostics,
  useLatestPhoto,
  useMyAccounts,
  useSchools,
  useSignCalendar,
  useSignStatus,
  useTimePref,
} from './queries'

function primaryLabel(state: HomeState): string | null {
  switch (state.primary) {
    case 'sign':
      return '再试一次'
    case 'resume':
      return '恢复账号'
    case 'edit':
      return '修改资料'
    case 'password':
      return '更新易班口令'
    case 'restore':
      return '恢复账号'
    case 'create':
      return '新建账号'
    default:
      return null
  }
}

function secondaryLabel(state: HomeState): string | null {
  switch (state.secondary) {
    case 'log':
      return '查看记录'
    case 'progress':
      return '查看进度'
    case 'contact':
      return '联系管理员'
    default:
      return null
  }
}

function signErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return '没有权限执行这个操作。'
    if (error.status === 404) return '账号不存在或已删除。'
    if (error.status === 429) return '操作太频繁，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    if (error.status === 409) {
      switch (error.code) {
        case 'window_closed':
          return '今天的签到窗口已经关闭。'
        case 'already_signed':
          return '今天已经签过，不需要再试。'
        case 'sign_running':
          return '正在签到，请稍候。'
        case 'task_ambiguous':
          return '账号有多条任务，请联系管理员。'
        default:
          return '当前状态不能签到。'
      }
    }
    return '签到没有成功，请稍后再试。'
  }
  return '网络异常，请稍后再试。'
}

function pauseErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '当前状态不允许这个操作。'
    if (error.status === 403) return '口令不正确，或没有权限。'
    if (error.status === 409) return '当前状态不允许这个操作。'
    if (error.status === 429) return '尝试过多，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    return '操作失败，请稍后再试。'
  }
  return '网络异常，请稍后再试。'
}

function statusClass(status: AccountStatus, deleted: boolean): string {
  const tone = userStatusTone(status, deleted)
  return `uapp-status uapp-status--${tone}`
}

function WindowAxis({
  geometry,
  failed,
  running = false,
}: {
  geometry: WindowGeometry
  failed: boolean
  running?: boolean
}) {
  return (
    <div className="uapp-window">
      <div className="uapp-window-head">
        <span>可签到窗口</span>
        <span>
          {geometry.start} – {geometry.end}
        </span>
      </div>
      <div className="uapp-window-track">
        <span
          className="uapp-window-band"
          style={{ left: `${geometry.bandLeft}%`, width: `${geometry.bandWidth}%` }}
        />
        {geometry.chosenLeft !== null ? (
          <span
            className={running ? 'uapp-window-chosen is-running' : 'uapp-window-chosen'}
            style={{ left: `${geometry.chosenLeft}%` }}
            aria-hidden="true"
          />
        ) : null}
        {geometry.actualLeft !== null ? (
          <span
            className={
              failed ? 'uapp-window-dot uapp-window-dot--danger' : 'uapp-window-dot'
            }
            style={{ left: `${geometry.actualLeft}%` }}
            aria-hidden="true"
          />
        ) : null}
      </div>
      <div className="uapp-window-scale">
        {geometry.scale.map((tick) => (
          <span key={tick}>{tick}</span>
        ))}
      </div>
      <div className="uapp-window-legend">
        {geometry.chosenText ? (
          <span className="uapp-window-legend-item">
            <i className="uapp-window-swatch uapp-window-swatch--chosen" aria-hidden="true" />
            你选 {geometry.chosenText}
          </span>
        ) : null}
        {geometry.actualText ? (
          <span className="uapp-window-legend-item">
            <i
              className={
                failed
                  ? 'uapp-window-swatch uapp-window-swatch--actual uapp-window-swatch--danger'
                  : 'uapp-window-swatch uapp-window-swatch--actual'
              }
              aria-hidden="true"
            />
            实际 {geometry.actualText}
          </span>
        ) : null}
        {!geometry.chosenText && !geometry.actualText ? (
          <span className="uapp-window-legend-item">{geometry.caption}</span>
        ) : null}
      </div>
    </div>
  )
}

function ResultHero({
  account,
  state,
  geometry,
  busy,
  error,
  nextTitle,
  nextNote,
  streak,
  successDays,
  recordedCount,
  running,
  onPrimary,
  onSecondary,
}: {
  account: Account
  state: HomeState
  geometry: WindowGeometry | null
  busy: boolean
  error: string | null
  nextTitle: string
  nextNote: string
  streak: number
  successDays: number
  recordedCount: number
  running: boolean
  onPrimary: () => void
  onSecondary: () => void
}) {
  const primary = primaryLabel(state)
  const secondary = secondaryLabel(state)
  const failed = state.tone === 'danger'
  return (
    <section className={`uapp-hero uapp-hero--${state.tone} uapp-result`} aria-label="今日签到">
      <div className="uapp-result-head">
        <span className="uapp-result-eyebrow">今日签到</span>
        <span className={statusClass(account.status, account.deleted_at !== null)}>
          <span className="uapp-status-dot" />
          {userStatusLabel(account.status, account.deleted_at !== null)}
        </span>
      </div>

      <div className="uapp-result-body">
        <div className="uapp-result-main" aria-live="polite">
          <h2 className="uapp-hero-title">{state.title}</h2>
          <div className="uapp-result-when">
            {state.time ? <span className="uapp-hero-time">{state.time}</span> : null}
            <p className="uapp-hero-note">{state.note}</p>
          </div>
          {error ? (
            <p className="uapp-field-error" role="alert">
              {error}
            </p>
          ) : null}
        </div>

        <div className="uapp-result-side">
          {geometry ? <WindowAxis geometry={geometry} failed={failed} running={running} /> : null}
        </div>
      </div>

      <div className="uapp-result-foot">
        <div className="uapp-result-stats">
          <div className="uapp-stat">
            <span className="uapp-stat-value">
              <AnimatedNumber value={streak} />
            </span>
            <span className="uapp-stat-label">连续成功 · 天</span>
          </div>
          <span className="uapp-stat-divider" aria-hidden="true" />
          <div className="uapp-stat">
            <span className="uapp-stat-value">
              {successDays}
              <i>/{recordedCount}</i>
            </span>
            <span className="uapp-stat-label">近 7 天成功</span>
          </div>
        </div>
        <div className="uapp-result-next">
          <span className="uapp-next-icon">
            <Icon name="clock" size={16} />
          </span>
          <div className="uapp-next-main">
            <div className="uapp-next-time">{nextTitle}</div>
            <div className="uapp-next-note">{nextNote}</div>
          </div>
        </div>
        {primary || secondary ? (
          <div className="uapp-hero-actions">
            {primary ? (
              <button
                type="button"
                className="uapp-button uapp-button--primary"
                disabled={busy}
                onClick={onPrimary}
              >
                {busy ? '处理中…' : primary}
              </button>
            ) : null}
            {secondary ? (
              <button type="button" className="uapp-hero-link" onClick={onSecondary}>
                {secondary} ›
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  )
}

function IdentityCard({
  account,
  schoolName,
  photoUrl,
  prefLabel,
  onEdit,
  onTimePref,
}: {
  account: Account
  schoolName: string
  photoUrl: string | null
  prefLabel: string
  onEdit: () => void
  onTimePref: () => void
}) {
  return (
    <section className="uapp-card uapp-identity" aria-label="账号身份">
      <div className="uapp-hero-identity">
        <div className={photoUrl ? 'uapp-hero-photo' : 'uapp-hero-photo uapp-hero-photo--empty'}>
          {photoUrl ? <img src={photoUrl} alt="现场照片" /> : <Icon name="user" size={22} />}
        </div>
        <div className="uapp-hero-identity-text">
          <h2 className="uapp-hero-name">{displayName(account.name, account.phone_masked)}</h2>
          <p className="uapp-hero-meta">{schoolName ? `${schoolName} · ` : ''}{account.phone_masked}</p>
        </div>
      </div>
      <div className="uapp-identity-fields">
        <div className="uapp-identity-row">
          <span className="uapp-identity-label">学校</span>
          <span className="uapp-identity-value">{schoolName || '未绑定'}</span>
        </div>
        <div className="uapp-identity-row">
          <span className="uapp-identity-label">偏好时间</span>
          <span className="uapp-identity-value">{prefLabel}</span>
        </div>
        <div className="uapp-identity-row">
          <span className="uapp-identity-label">现场照片</span>
          <span className="uapp-identity-value">{photoUrl ? '已准备' : '未上传'}</span>
        </div>
      </div>
      <div className="uapp-identity-actions">
        <button type="button" className="uapp-button uapp-button--secondary uapp-identity-edit" onClick={onEdit}>
          管理账号
        </button>
        <button type="button" className="uapp-hero-link uapp-identity-sub-link" onClick={onTimePref}>
          调整时间偏好 ›
        </button>
      </div>
    </section>
  )
}

function EmptyHero({ onCreate }: { onCreate: () => void }) {
  return (
    <section className="uapp-hero uapp-hero--info" aria-label="还没有签到账号">
      <h2 className="uapp-hero-title">还没有签到账号</h2>
      <p className="uapp-hero-note">
        添加后每天自动签到；审核通过前不会执行。
      </p>
      <div className="uapp-hero-actions">
        <button type="button" className="uapp-button uapp-button--primary" onClick={onCreate}>
          新建账号
        </button>
      </div>
    </section>
  )
}

function AnomalyHero({ onRetry }: { onRetry: () => void }) {
  return (
    <section className="uapp-hero uapp-hero--danger" aria-label="账号数据异常">
      <h2 className="uapp-hero-title">账号数据异常</h2>
      <p className="uapp-hero-note">
        检测到多个活跃账号。请联系管理员处理后再继续。
      </p>
      <div className="uapp-hero-actions">
        <button type="button" className="uapp-button uapp-button--secondary" onClick={onRetry}>
          刷新
        </button>
      </div>
    </section>
  )
}

function SkeletonHome() {
  return (
    <div className="uapp-page uapp-page--home" aria-busy="true">
      <div className="uapp-skeleton" style={{ width: 210, height: 38 }} />
      <div className="uapp-skeleton" style={{ width: 160, height: 14, marginTop: 12 }} />
      <div
        className="uapp-skeleton"
        style={{ width: '100%', height: 330, marginTop: 24, borderRadius: 28 }}
      />
      <div className="uapp-skeleton" style={{ width: '100%', height: 96, marginTop: 20 }} />
    </div>
  )
}

export function HomeView() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [fast, setFast] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [helpOpen, setHelpOpen] = useState(false)
  const [helpReturnFocus, setHelpReturnFocus] = useState<HTMLElement | null>(null)
  const [announcementDismissed, setAnnouncementDismissed] = useState(false)
  const fastTimer = useRef<number | null>(null)

  const accountsQuery = useMyAccounts()
  const schoolsQuery = useSchools()
  const signStatusQuery = useSignStatus(fast)
  const diagnosticsQuery = useDiagnostics(fast)
  const timePrefQuery = useTimePref()
  const announcementQuery = useAnnouncement()

  const accounts = accountsQuery.data?.items ?? []
  const liveAccounts = accounts.filter((item) => item.deleted_at === null)
  const live = liveAccounts[0] ?? null
  const deleted = live === null ? (accounts.find((item) => item.deleted_at !== null) ?? null) : null
  const account = live ?? deleted
  const anomaly = liveAccounts.length > 1

  const today = todayKey(new Date())
  const from = todayKey(new Date(Date.now() - 6 * 24 * 60 * 60 * 1000))
  const calendarQuery = useSignCalendar(from, today, account !== null)
  const photoQuery = useLatestPhoto(account?.id ?? null)

  const schools = schoolsQuery.data ?? []
  const schoolName = account
    ? (schools.find((item) => item.id === account.school_id)?.name ?? '')
    : ''

  const statusItem =
    signStatusQuery.data?.items.find((item) => item.account_id === account?.id) ?? null
  const diagnosticItem =
    diagnosticsQuery.data?.items.find((item) => item.account_id === account?.id) ?? null
  const todayState = parseTodayState(statusItem?.state)
  const state = homeState(
    account?.status ?? null,
    account?.deleted_at != null,
    todayState,
    diagnosticItem?.suggestion ?? null,
    statusItem?.run_at ?? null,
  )
  const showWindow =
    account?.status === 'active' && account.deleted_at === null && todayState !== null
  const geometry = showWindow
    ? windowGeometry(timePrefQuery.data, statusItem?.run_at ?? null)
    : null

  // 首屏仪表统计：只由近 7 天日历推导，不虚构月/均值。
  const calDays = calendarQuery.data?.days ?? []
  const recordedDays = calDays.filter((day) => day.total > 0)
  const successDays = recordedDays.filter((day) => day.success > 0).length
  let streak = 0
  for (let i = calDays.length - 1; i >= 0; i--) {
    const day = calDays[i]
    if (day.total === 0) continue
    if (day.success > 0) streak++
    else break
  }

  const photo = photoQuery.data?.items[0] ?? null
  const photoUrl = photo ? photoBytesPath(photo.id) : null

  const announcement = announcementQuery.data
  const showAnnouncement =
    !announcementDismissed && announcement !== undefined && announcement.text.trim() !== ''

  useEffect(() => {
    return () => {
      if (fastTimer.current !== null) window.clearTimeout(fastTimer.current)
    }
  }, [])

  // 执行中状态自动快轮询，让用户不刷新也能看到 running → 终态的落点。
  const running = todayState === 'running'
  useEffect(() => {
    if (running) {
      setFast(true)
    } else if (fastTimer.current === null) {
      setFast(false)
    }
  }, [running])

  const armFast = () => {
    setFast(true)
    if (fastTimer.current !== null) window.clearTimeout(fastTimer.current)
    fastTimer.current = window.setTimeout(() => setFast(false), 60_000)
  }

  const signMutation = useMutation({
    mutationFn: (id: string) => signAccount(id),
    onSuccess: () => {
      setActionError(null)
      armFast()
      notifySuccess('已提交，等待结果')
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.signStatus })
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.diagnostics })
    },
    onError: (error) => setActionError(signErrorText(error)),
  })

  const pauseMutation = useMutation({
    mutationFn: ({ id, paused }: { id: string; paused: boolean }) =>
      pauseAccount(id, { paused }),
    onSuccess: (_data, variables) => {
      setActionError(null)
      notifySuccess(variables.paused ? '已暂停签到' : '已恢复签到')
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.accounts })
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.signStatus })
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.diagnostics })
    },
    onError: (error) => setActionError(pauseErrorText(error)),
  })

  const busy = signMutation.isPending || pauseMutation.isPending

  const goCreate = () => void navigate({ to: '/app/account/new' })
  const goAccount = () => void navigate({ to: '/app/account' })
  const goHistoryToday = () =>
    void navigate({ to: '/app/history', search: { day: today } })

  const primaryClick = () => {
    if (!account) {
      goCreate()
      return
    }
    switch (state.primary) {
      case 'sign':
        signMutation.mutate(account.id)
        break
      case 'resume':
        pauseMutation.mutate({ id: account.id, paused: false })
        break
      case 'edit':
      case 'password':
      case 'restore':
        goAccount()
        break
      case 'create':
        goCreate()
        break
      default:
        break
    }
  }

  const secondaryClick = () => {
    switch (state.secondary) {
      case 'log':
        goHistoryToday()
        break
      case 'progress':
        goAccount()
        break
      case 'contact':
        setHelpReturnFocus(
          document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null,
        )
        setHelpOpen(true)
        break
      default:
        break
    }
  }

  if (accountsQuery.isPending) return <SkeletonHome />

  if (accountsQuery.isError) {
    return (
      <div className="uapp-page uapp-page--home">
        <h1 className="uapp-greeting">今天签到了吗？</h1>
        <section className="uapp-hero uapp-hero--danger" aria-label="加载失败">
          <h2 className="uapp-hero-title">账号读取失败</h2>
          <p className="uapp-hero-note">请检查网络后重试。</p>
          <div className="uapp-hero-actions">
            <button
              type="button"
              className="uapp-button uapp-button--secondary"
              onClick={() => void accountsQuery.refetch()}
            >
              重试
            </button>
          </div>
        </section>
      </div>
    )
  }

  const nextTitle =
    diagnosticItem?.next_retry_at && todayState === 'failed'
      ? `下次重试 ${formatClock(diagnosticItem.next_retry_at)}`
      : account?.status === 'pending'
        ? '审核通过后自动开始'
        : account?.status === 'paused_admin' || account?.status === 'paused_user'
          ? '恢复后从下一次开始'
          : nextRunText(timePrefQuery.data)
  const nextNote = timePrefQuery.data?.window
    ? `可签到时间 ${timePrefQuery.data.window.start}–${timePrefQuery.data.window.end}`
    : '到点会自动开始'

  return (
    <div className="uapp-page uapp-page--home">
      <div className="uapp-home-head">
        <div className="uapp-date-row uapp-home-eyebrow">
          <span>{businessDateLabel(new Date())}</span>
        </div>
        <h1 className="uapp-greeting">{greetingFor(new Date())}</h1>
      </div>

      {showAnnouncement ? (
        <div className="uapp-announcement" role="status">
          <span className="uapp-announcement-dot" />
          <span>{announcement.text}</span>
          <button
            type="button"
            className="uapp-announcement-close"
            aria-label="关闭公告"
            onClick={() => setAnnouncementDismissed(true)}
          >
            ×
          </button>
        </div>
      ) : null}

      <div className="uapp-home-bento">
        <div className="uapp-home-result">
          {anomaly ? (
            <AnomalyHero onRetry={() => void accountsQuery.refetch()} />
          ) : account && signStatusQuery.isError ? (
            <section className="uapp-hero uapp-hero--danger" aria-label="签到状态读取失败">
              <h2 className="uapp-hero-title">签到状态读取失败</h2>
              <p className="uapp-hero-note">
                这不等于今天不用签到；请重试，或到账号页查看当前状态。
              </p>
              <div className="uapp-hero-actions">
                <button
                  type="button"
                  className="uapp-button uapp-button--secondary"
                  onClick={() => void signStatusQuery.refetch()}
                >
                  重试
                </button>
              </div>
            </section>
          ) : account ? (
            <ResultHero
              account={account}
              state={state}
              geometry={geometry}
              busy={busy}
              error={actionError}
              nextTitle={nextTitle}
              nextNote={nextNote}
              streak={streak}
              successDays={successDays}
              recordedCount={recordedDays.length}
              running={running}
              onPrimary={primaryClick}
              onSecondary={secondaryClick}
            />
          ) : (
            <EmptyHero onCreate={goCreate} />
          )}
        </div>

        {account && !anomaly ? (
          <div className="uapp-home-identity">
            <IdentityCard
              account={account}
              schoolName={schoolName}
              photoUrl={photoUrl}
              prefLabel={geometry?.chosenText ? `${geometry.chosenText} 左右` : nextRunText(timePrefQuery.data)}
              onEdit={goAccount}
              onTimePref={() => void navigate({ to: '/app/settings/time-pref' })}
            />
          </div>
        ) : null}

        {account && !anomaly && recordedDays.length > 0 ? (
          <div className="uapp-home-cal">
            <section className="uapp-card uapp-home-cal-card" aria-label="签到日历">
              <WeekStrip days={calDays} today={today} />
            </section>
          </div>
        ) : null}
      </div>

      <Modal
        open={helpOpen}
        onOpenChange={setHelpOpen}
        title="联系管理员"
        returnFocusTo={helpReturnFocus}
        description="请把账号手机号后四位与失败时间发给部署方管理员。"
        footer={
          <button
            type="button"
            className="uapp-button uapp-button--secondary"
            onClick={() => setHelpOpen(false)}
          >
            知道了
          </button>
        }
      >
        <p className="uapp-hero-note">
          管理端不能看到你的易班口令或完整手机号；不要通过聊天发送口令。
        </p>
      </Modal>
    </div>
  )
}
