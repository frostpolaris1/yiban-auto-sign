/**
 * **功能**
 * 用户端「今天」页：结果、窗口标尺、近七日、身份与提示。
 *
 * **归属**
 * user-next/pages 路由页；取数仍复用 features/me 的查询钩子。
 *
 * **口径**
 * 一屏先回答「今天签上了吗」；无账号、异常、读取失败都给出下一步。
 */

import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { Link } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Modal } from '@/components/ui/Modal'
import { notifySuccess } from '@/components/ui/toast'
import { cx } from '@/lib/cx'
import { pauseAccount, signAccount } from '@/lib/api/account'
import { photoBytesPath } from '@/lib/api/photo'
import {
  displayName,
  parseTodayState,
  userStatusLabel,
  userStatusTone,
} from '@/lib/account-display'
import {
  nextRunText,
  todayKey,
  weekdayLabel,
} from '@/features/me/display'
import { formatClock } from '@/lib/format'
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
} from '@/features/me/queries'
import { pauseErrorText, signErrorText } from '../lib/errors'
import {
  rulerGeometry,
  successDetail,
  todayStory,
  type RulerGeometry,
} from '../lib/today'

const DAY_MS = 24 * 60 * 60 * 1000

function shiftDay(key: string, delta: number): string {
  const date = new Date(key + 'T12:00:00+08:00')
  return new Date(date.getTime() + delta * DAY_MS).toISOString().slice(0, 10)
}

function primaryLabel(primary: ReturnType<typeof todayStory>['primary']): string {
  switch (primary) {
    case 'sign':
      return '再试一次'
    case 'resume':
      return '恢复签到账号'
    case 'edit':
      return '修改资料'
    case 'password':
      return '更新易班口令'
    case 'restore':
      return '恢复签到账号'
    case 'create':
      return '新建账号'
    default:
      return ''
  }
}

export function TodayPage() {
  const queryClient = useQueryClient()
  const accountsQuery = useMyAccounts()
  const schoolsQuery = useSchools()
  const timePrefQuery = useTimePref()
  const [fast, setFast] = useState(false)
  const fastTimer = useRef<number | null>(null)
  const signStatusQuery = useSignStatus(fast)
  const diagnosticsQuery = useDiagnostics(fast)
  const today = todayKey(new Date())
  const calendarQuery = useSignCalendar(shiftDay(today, -6), today)
  const announcementQuery = useAnnouncement()
  const [announcementDismissed, setAnnouncementDismissed] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const items = accountsQuery.data?.items ?? []
  const liveAccounts = items.filter((item) => item.deleted_at === null)
  const anomaly = liveAccounts.length > 1
  const account = liveAccounts[0] ?? null
  const deleted = items.find((item) => item.deleted_at !== null) ?? null
  const current = account ?? deleted
  const statusItem = current
    ? signStatusQuery.data?.items.find((item) => item.account_id === current.id)
    : undefined
  const diagnosticItem = current
    ? diagnosticsQuery.data?.items.find((item) => item.account_id === current.id)
    : undefined
  const todayState = parseTodayState(statusItem?.state)
  const timePref = timePrefQuery.data
  const geometry =
    current && current.deleted_at === null && todayState !== null
      ? rulerGeometry(timePref, statusItem?.run_at ?? null)
      : null
  const chosenText = geometry?.chosenText ?? ''
  const actualText = formatClock(statusItem?.run_at ?? null)
  const story = current
    ? todayStory(
        current.status,
        current.deleted_at !== null,
        todayState,
        diagnosticItem?.suggestion ?? null,
        diagnosticItem?.attempts ?? 0,
        chosenText,
        actualText,
      )
    : null
  const detail =
    story?.tone === 'success'
      ? successDetail(
          diagnosticItem?.attempts ?? 0,
          chosenText,
          actualText,
        )
      : story?.detail ?? story?.note ?? null
  const showClock =
    statusItem !== undefined &&
    statusItem.state !== null &&
    statusItem.state !== 'pending' &&
    statusItem.state !== 'running'
  const running = todayState === 'running'
  const photoQuery = useLatestPhoto(account?.id ?? null)
  const photo = photoQuery.data?.items[0] ?? null
  const photoUrl = photo ? photoBytesPath(photo.id) : null
  const schoolName =
    schoolsQuery.data?.find((school) => school.id === current?.school_id)?.name ??
    (schoolsQuery.isError
      ? '学校信息暂时读不到'
      : schoolsQuery.data
        ? '学校未登记'
        : '学校信息加载中')
  const announcement = announcementQuery.data
  const showAnnouncement =
    !announcementDismissed &&
    announcement !== undefined &&
    announcement.text.trim() !== ''

  const days = calendarQuery.data?.days ?? []
  let streak = 0
  let streakIndex = days.length - 1
  while (streakIndex >= 0 && days[streakIndex].total === 0) streakIndex -= 1
  for (; streakIndex >= 0; streakIndex -= 1) {
    const day = days[streakIndex]
    if (day.total > 0 && day.success > 0 && day.failed === 0) streak += 1
    else break
  }
  const weekDays = days.slice(-7)
  const weekRecorded = weekDays.filter((day) => day.total > 0)
  const weekSuccessDays = weekRecorded.filter((day) => day.success > 0 && day.failed === 0).length

  useEffect(() => {
    return () => {
      if (fastTimer.current !== null) window.clearTimeout(fastTimer.current)
    }
  }, [])

  useEffect(() => {
    if (fastTimer.current !== null) {
      window.clearTimeout(fastTimer.current)
      fastTimer.current = null
    }
    if (running) {
      setFast(true)
      return
    }
    // A finished run can turn into a pending one; keep the faster cadence for
    // one more minute after the status changes, then fall back to the normal poll.
    fastTimer.current = window.setTimeout(() => {
      fastTimer.current = null
      setFast(false)
    }, 60_000)
    return () => {
      if (fastTimer.current !== null) {
        window.clearTimeout(fastTimer.current)
        fastTimer.current = null
      }
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

  const resumeMutation = useMutation({
    mutationFn: (id: string) => pauseAccount(id, { paused: false }),
    onSuccess: () => {
      setActionError(null)
      notifySuccess('已恢复签到')
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.accounts })
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.signStatus })
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.diagnostics })
    },
    onError: (error) => setActionError(pauseErrorText(error)),
  })

  const busy = signMutation.isPending || resumeMutation.isPending

  const primaryClick = () => {
    if (!current) return
    if (story?.primary === 'resume') {
      resumeMutation.mutate(current.id)
      return
    }
    if (story?.primary === 'sign') signMutation.mutate(current.id)
  }

  const nextTitle =
    diagnosticItem?.next_retry_at && todayState === 'failed'
      ? '下次重试 ' + formatClock(diagnosticItem.next_retry_at)
      : current?.status === 'pending'
        ? '审核通过后自动开始'
        : current?.status === 'paused_admin' || current?.status === 'paused_user'
          ? '恢复后从下一次开始'
          : nextRunText(timePref)

  const helpReturnFocus = useRef<HTMLElement | null>(null)
  const openHelp = (event: ReactMouseEvent<HTMLButtonElement>) => {
    helpReturnFocus.current = event.currentTarget
    setHelpOpen(true)
  }

  if (accountsQuery.isPending) {
    return (
      <div className="un-today">
        <div className="un-today__dateline">
          <span className="un-skeleton un-skeleton--line" />
        </div>
        <div className="un-skeleton un-skeleton--hero" />
      </div>
    )
  }

  if (accountsQuery.isError) {
    return (
      <div className="un-today">
        <div className="un-today__dateline">
          <p className="un-eyebrow">今天</p>
        </div>
        <section className="un-result un-result--danger" aria-label="读取失败">
          <h1 className="un-result__status">账号读取失败</h1>
          <p className="un-result__detail">请检查网络后重试；这不代表今天不用签到。</p>
          <div className="un-result__actions">
            <button
              type="button"
              className="un-btn un-btn--secondary"
              onClick={() => void accountsQuery.refetch()}
            >
              重试
            </button>
          </div>
        </section>
      </div>
    )
  }

  if (signStatusQuery.isPending) {
    return (
      <div className="un-today">
        <div className="un-today__dateline">
          <span className="un-skeleton un-skeleton--line" />
        </div>
        <div
          className="un-skeleton un-skeleton--hero"
          aria-busy="true"
          aria-label="正在读取今天的签到状态"
        />
      </div>
    )
  }

  return (
    <div className="un-today">
      <div className="un-today__dateline">
        <p className="un-today__date">{formatDateLine(new Date())}</p>
        {timePref?.window ? (
          <p className="un-today__window">
            晨间窗口 {timePref.window.start}–{timePref.window.end}
          </p>
        ) : null}
      </div>

      {showAnnouncement ? (
        <div className="un-announcement uapp-announcement" role="status">
          <span className="un-announcement__dot" aria-hidden="true" />
          <p>{announcement.text}</p>
          <button
            type="button"
            className="un-announcement__close uapp-announcement-close"
            aria-label="关闭公告"
            onClick={() => setAnnouncementDismissed(true)}
          >
            ×
          </button>
        </div>
      ) : null}

      {anomaly ? (
        <section className="un-result un-result--warning" aria-labelledby="un-anomaly-title">
          <p className="un-eyebrow">需要处理</p>
          <h1 id="un-anomaly-title" className="un-result__status uapp-hero-title">
            发现多个账号
          </h1>
          <p className="un-result__detail">
            同时存在多个账号会让自动签到不确定。请保留一个，其余联系管理员处理。
          </p>
          <div className="un-result__actions">
            <button
              type="button"
              className="un-btn un-btn--secondary"
              onClick={() => void accountsQuery.refetch()}
            >
              重新检查
            </button>
            <button
              type="button"
              className="un-btn un-btn--ghost"
              onClick={openHelp}
            >
              联系管理员
            </button>
          </div>
        </section>
      ) : !current ? (
        <section className="un-result un-result--empty" aria-labelledby="un-empty-title">
          <p className="un-eyebrow">第一步</p>
          <h1 id="un-empty-title" className="un-result__status uapp-hero-title">
            还没有签到账号
          </h1>
          <p className="un-result__detail">
            绑定易班账号并选择时间窗口，之后每天由系统自动签到。
          </p>
          <div className="un-result__actions">
            <Link to="/app/me/account/new" className="un-btn un-btn--primary">
              新建账号
            </Link>
            <button
              type="button"
              className="un-btn un-btn--ghost"
              onClick={openHelp}
            >
              先了解流程
            </button>
          </div>
        </section>
      ) : signStatusQuery.isError ? (
        <section className="un-result un-result--danger" aria-labelledby="un-status-error">
          <p className="un-eyebrow">今天</p>
          <h1 id="un-status-error" className="un-result__status uapp-hero-title">
            签到状态读取失败
          </h1>
          <p className="un-result__detail">
            这不等于今天不用签到；请重试，或到「我的」查看账号状态。
          </p>
          <div className="un-result__actions">
            <button
              type="button"
              className="un-btn un-btn--secondary"
              onClick={() => void signStatusQuery.refetch()}
            >
              重试
            </button>
            <Link to="/app/me/account" className="un-btn un-btn--ghost">
              查看账号
            </Link>
          </div>
        </section>
      ) : (
        <div className="un-today__grid">
          <div className="un-today__primary">
            <section
              className={cx('un-result', 'uapp-hero', 'un-shared-result')}
              aria-labelledby="un-result-title"
              data-tone={story?.tone ?? 'info'}
            >
              <div className="un-result__head">
                <h1 id="un-result-title" className="un-result__status uapp-hero-title uapp-greeting">
                  <span
                    className={cx('un-result__dot', 'uapp-status', 'uapp-status--' + (story?.tone ?? 'info'))}
                    data-tone={story?.tone ?? 'info'}
                    aria-hidden="true"
                  />
                  {story?.title}
                </h1>
                <Link
                  to="/app/history"
                  search={{ day: today }}
                  className="un-result__log"
                >
                  今天的记录 <span aria-hidden="true">→</span>
                </Link>
              </div>

              {showClock ? (
                <div className="un-result__time">
                  <time className="un-result__clock un-serif uapp-hero-time" dateTime={statusItem?.run_at ?? undefined}>
                    {formatClock(statusItem?.run_at ?? null)}
                  </time>
                  <span className="un-result__zone">上海时间</span>
                </div>
              ) : (
                <p className="un-result__pending">
                  {todayState === 'running' ? '正在签到，结果马上回来。' : '今天还没有执行记录。'}
                </p>
              )}

              <p className="un-result__detail">{detail}</p>

              {actionError ? (
                <p className="un-result__error" role="alert">
                  {actionError}
                </p>
              ) : null}

              <div className="un-result__actions uapp-hero-actions">
                {story?.primary ? (
                  story.primary === 'create' ? (
                    <Link to="/app/me/account/new" className="un-btn un-btn--primary">
                      {primaryLabel(story.primary)}
                    </Link>
                  ) : story.primary === 'sign' || story.primary === 'resume' ? (
                    <button
                      type="button"
                      className="un-btn un-btn--primary"
                      disabled={busy}
                      onClick={primaryClick}
                    >
                      {busy ? '执行中…' : primaryLabel(story.primary)}
                    </button>
                  ) : (
                    <Link to="/app/me/account" className="un-btn un-btn--primary">
                      {primaryLabel(story.primary)}
                    </Link>
                  )
                ) : null}
                {story?.secondary === 'contact' ? (
                  <button
                    type="button"
                    className="un-btn un-btn--ghost"
                    onClick={openHelp}
                  >
                    联系管理员
                  </button>
                ) : story?.secondary === 'progress' ? (
                  <Link to="/app/me/account" className="un-btn un-btn--ghost">
                    查看进度
                  </Link>
                ) : null}
              </div>
            </section>

            {geometry ? (
              <Ruler
                geometry={geometry}
                failed={story?.tone === 'danger'}
                running={running}
              />
            ) : null}

            {weekDays.length > 0 ? (
              <section className="un-week" aria-labelledby="un-week-title">
                <header className="un-week__head">
                  <h2 id="un-week-title" className="un-week__title">
                    近 7 天
                  </h2>
                  <p className="un-week__summary">
                    <span className="un-week__count">{weekSuccessDays}</span>
                    <span className="un-week__of">/{weekDays.length || 7}</span>
                    <span className="un-week__cap">天全部成功</span>
                    {streak > 0 ? (
                      <span className="un-week__streak">· 连续 {streak} 天</span>
                    ) : null}
                  </p>
                </header>
                <ol className="un-week__days">
                  {weekDays.map((day) => {
                    const isToday = day.day === today
                    const tone =
                      day.failed > 0
                        ? 'danger'
                        : day.success > 0
                          ? 'success'
                          : day.skipped > 0
                            ? 'warning'
                            : 'none'
                    const outcome =
                      tone === 'success'
                        ? '成功'
                        : tone === 'danger'
                          ? '有失败'
                          : tone === 'warning'
                            ? '未执行'
                            : '无记录'
                    return (
                      <li
                        key={day.day}
                        className={cx('un-week__day', isToday && 'is-today')}
                      >
                        <Link
                          to="/app/history"
                          search={{ day: day.day }}
                          className="un-week__link"
                          aria-label={day.day + ' 的记录，' + outcome}
                        >
                          <span className="un-week__weekday">{weekdayLabel(day.day)}</span>
                          <span className="un-week__date un-serif">{day.day.slice(8)}</span>
                          <span
                            className={cx('un-week__dot', 'is-' + tone)}
                            aria-hidden="true"
                          />
                        </Link>
                      </li>
                    )
                  })}
                </ol>
              </section>
            ) : null}
          </div>

          <aside className="un-today__aside" aria-label="账号与提示">
            <Link
              to="/app/me/account"
              className="un-identity uapp-identity un-shared-identity"
              aria-label="查看签到账号"
            >
              <div className="un-identity__head">
                <span className="un-avatar un-avatar--lg" aria-hidden="true">
                  {photoUrl ? (
                    <img src={photoUrl} alt="" className="un-avatar__img" />
                  ) : (
                    <span className="un-serif">{monogram(current.name)}</span>
                  )}
                </span>
                <div className="un-identity__name-wrap">
                  <h2 className="un-identity__name uapp-hero-name">
                    {displayName(current.name, current.phone_masked)}
                  </h2>
                  <span
                    className={cx(
                      'un-chip',
                      'uapp-status',
                      'un-chip--' + chipTone(userStatusTone(current.status, current.deleted_at !== null)),
                    )}
                  >
                    {userStatusLabel(current.status, current.deleted_at !== null)}
                  </span>
                </div>
              </div>
              <dl className="un-identity__rows">
                <div>
                  <dt>学校</dt>
                  <dd title={schoolName}>{schoolName}</dd>
                </div>
                <div>
                  <dt>绑定手机</dt>
                  <dd>{current.phone_masked}</dd>
                </div>
                <div>
                  <dt>预选时间</dt>
                  <dd>
                    {geometry?.chosenText ? geometry.chosenText + ' 左右' : '按窗口安排'}
                  </dd>
                </div>
                <div>
                  <dt>下次执行</dt>
                  <dd>{nextTitle}</dd>
                </div>
              </dl>
              <span className="un-identity__foot">
                签到账号 <span aria-hidden="true">›</span>
              </span>
            </Link>

            <aside className="un-note">
              <p className="un-note__label">今日提示</p>
              <p className="un-note__text">{noteLine(story?.tone, todayState, photo !== null)}</p>
            </aside>
          </aside>
        </div>
      )}

      <Modal
        open={helpOpen}
        onOpenChange={setHelpOpen}
        title="联系管理员"
        returnFocusTo={helpReturnFocus.current}
        className="un-dialog"
        description="把账号手机号后四位与失败时间发给部署方管理员；不要发送易班口令。"
        footer={
          <button
            type="button"
            className="un-btn un-btn--secondary"
            onClick={() => setHelpOpen(false)}
          >
            知道了
          </button>
        }
      >
        <p className="un-dialog__copy">
          管理端看不到你的易班口令或完整手机号。遇到持续失败，先重试一次，再把时间告诉管理员。
        </p>
      </Modal>
    </div>
  )
}

function chipTone(tone: string): string {
  return tone === 'info' ? 'blue' : tone
}

function monogram(name: string): string {
  const first = Array.from(name.trim())[0]
  return first ?? '我'
}

function noteLine(
  tone: string | undefined,
  todayState: string | null,
  hasPhoto: boolean,
): string {
  if (todayState === 'running') return '正在执行；结果会在几秒内更新，不需要刷新。'
  if (todayState === 'failed') return '先重试一次；如果仍然失败，把失败时间告诉管理员。'
  if (tone === 'warning') return '审核或资料处理完成后，系统会按窗口自动开始。'
  if (tone === 'success') {
    return hasPhoto
      ? '现场照片已就绪；签到时间可在「我的」调整。'
      : '还没有现场照片；可在账号页补一张，供审核时核对。'
  }
  return '系统会在每天的时间窗口内自动执行；这里只展示结果。'
}

function formatDateLine(date: Date): string {
  const month = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: 'long',
    day: 'numeric',
  }).format(date)
  const weekday = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    weekday: 'long',
  }).format(date)
  return month + ' ' + weekday
}

function Ruler({
  geometry,
  failed,
  running,
}: {
  geometry: RulerGeometry
  failed: boolean
  running: boolean
}) {
  return (
    <section className="un-ruler uapp-window" aria-labelledby="un-ruler-title">
      <div className="un-ruler__head">
        <h2 id="un-ruler-title" className="un-ruler__title">
          可签到窗口
        </h2>
        <p className="un-ruler__range">
          {geometry.start} – {geometry.end}
        </p>
      </div>
      <div className="un-ruler__track uapp-window-scale">
        <span
          className="un-ruler__band"
          style={{
            left: geometry.bandLeft + '%',
            width: geometry.bandWidth + '%',
          }}
          aria-hidden="true"
        />
        {geometry.nowLeft !== null ? (
          <span
            className="un-ruler__now"
            style={{ left: geometry.nowLeft + '%' }}
            aria-hidden="true"
          />
        ) : null}
        {geometry.chosenLeft !== null && geometry.chosenText !== null ? (
          <span
            className="un-ruler__callout is-chosen"
            style={{ left: geometry.chosenLeft + '%' }}
            aria-hidden="true"
          >
            {geometry.chosenText}
          </span>
        ) : null}
        {geometry.actualLeft !== null && geometry.actualText !== null ? (
          <span
            className="un-ruler__callout is-actual"
            style={{ left: geometry.actualLeft + '%' }}
            aria-hidden="true"
          >
            {geometry.actualText}
          </span>
        ) : null}
        {geometry.nowLeft !== null ? (
          <span
            className="un-ruler__callout is-now"
            style={{ left: geometry.nowLeft + '%' }}
            aria-hidden="true"
          >
            现在
          </span>
        ) : null}
        {geometry.chosenLeft !== null ? (
          <span
            className={cx('un-ruler__pin', 'is-chosen', running && 'is-running')}
            style={{ left: geometry.chosenLeft + '%' }}
            aria-hidden="true"
          />
        ) : null}
        {geometry.actualLeft !== null ? (
          <span
            className={cx('un-ruler__pin', 'is-actual', failed && 'is-failed')}
            style={{ left: geometry.actualLeft + '%' }}
            aria-hidden="true"
          />
        ) : null}
        {geometry.ticks.map((tick) => (
          <span
            key={tick.text}
            className={cx(
              'un-ruler__tick',
              'uapp-ruler-tick',
              tick.major && 'is-major',
              tick.bound && 'is-bound',
              tick.left <= 0 && 'is-start',
              tick.left >= 100 && 'is-end',
            )}
            style={{ left: tick.left + '%' }}
          >
            <span className="un-ruler__tick-mark" aria-hidden="true" />
            <span className="un-ruler__tick-text">{tick.text}</span>
          </span>
        ))}
      </div>
      <div className="un-ruler__legend uapp-window-legend">
        {geometry.chosenText !== null ? (
          <span className="un-ruler__legend-item">
            <span className="un-ruler__key is-chosen" aria-hidden="true" />
            你选 {geometry.chosenText}
          </span>
        ) : (
          <span className="un-ruler__legend-item is-muted">
            <span className="un-ruler__key is-chosen" aria-hidden="true" />
            系统按窗口自动分配
          </span>
        )}
        <span className="un-ruler__legend-item">
          <span className="un-ruler__key is-actual" aria-hidden="true" />
          实际 {geometry.actualText ?? '—'}
        </span>
        {geometry.nowLeft !== null ? (
          <span className="un-ruler__legend-item is-muted">
            <span className="un-ruler__key is-now" aria-hidden="true" />
            现在
          </span>
        ) : null}
      </div>
    </section>
  )
}
