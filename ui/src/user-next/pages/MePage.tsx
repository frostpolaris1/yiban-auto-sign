/**
 * **功能**
 * 用户端「我的」页：档案入口、近况、偏好与安全。
 *
 * **归属**
 * user-next/pages；账号与偏好数据仍来自 features/me 查询。
 *
 * **口径**
 * 档案优先；危险操作与退出登录放在右侧独立分区，不和偏好混排。
 */

import { useState } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { notifySuccess } from '@/components/ui/toast'
import { cx } from '@/lib/cx'
import { deleteMyAccount, logout } from '@/lib/api/auth'
import { ApiError } from '@/lib/api/client'
import { setMailNotify, setPhotoRetention } from '@/lib/api/me'
import { photoBytesPath } from '@/lib/api/photo'
import {
  displayName,
  userStatusLabel,
  userStatusTone,
} from '@/lib/account-display'
import { clearSessionExpired, useMe } from '@/lib/session'
import {
  ME_KEYS,
  useLatestPhoto,
  useMailNotify,
  useMyAccounts,
  usePhotoRetention,
  useSchools,
  useSignCalendar,
  useTimePref,
} from '@/features/me/queries'
import {
  nextRunText,
  schoolLabel,
  todayKey,
  weekdayLabel,
} from '@/features/me/display'
import { ReauthDialog } from '@/features/me/ReauthDialog'

const RETENTION_OPTIONS = [30, 90, 180, 365] as const

function shiftDay(key: string, delta: number): string {
  const date = new Date(key + 'T12:00:00+08:00')
  return new Date(date.getTime() + delta * 86_400_000).toISOString().slice(0, 10)
}

function chipTone(tone: string): string {
  return tone === 'info' ? 'blue' : tone
}

function deleteErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '请输入本站登录口令。'
    if (error.status === 403) return '口令不正确。'
    if (error.status === 429) return '尝试过多，请稍后再试。'
    return '删除没有完成，请稍后再试。'
  }
  return '网络异常，请稍后再试。'
}

export function MePage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const me = useMe()
  const accountsQuery = useMyAccounts()
  const schoolsQuery = useSchools()
  const timePrefQuery = useTimePref()
  const mailQuery = useMailNotify()
  const retentionQuery = usePhotoRetention()
  const today = todayKey(new Date())
  const calendarQuery = useSignCalendar(shiftDay(today, -29), today)
  const [error, setError] = useState<string | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleteReturnFocus, setDeleteReturnFocus] = useState<HTMLElement | null>(null)

  const items = accountsQuery.data?.items ?? []
  const live = items.filter((item) => item.deleted_at === null)
  const account = live.find((item) => item.status === 'active') ?? live[0] ?? null
  const photoQuery = useLatestPhoto(account?.id ?? null)
  const photo = photoQuery.data?.items[0] ?? null
  const photoUrl = photo ? photoBytesPath(photo.id) : null
  const schoolName = schoolLabel(
    schoolsQuery.data,
    schoolsQuery.isError,
    account?.school_id,
  )
  const days = calendarQuery.data?.days ?? []
  const recorded = days.filter((day) => day.total > 0)
  const successDays = recorded.filter((day) => day.success > 0 && day.failed === 0).length
  const failedDays = recorded.filter((day) => day.failed > 0).length
  let streak = 0
  let streakIndex = days.length - 1
  // 今天还没有任务时从最近一个有任务的日期起算，不把“尚未执行”误判为断签。
  while (streakIndex >= 0 && days[streakIndex].total === 0) streakIndex -= 1
  for (; streakIndex >= 0; streakIndex -= 1) {
    const day = days[streakIndex]
    if (day.total > 0 && day.success > 0 && day.failed === 0) streak += 1
    else break
  }
  const pref = timePrefQuery.data
  const chosenSlot =
    pref?.slots?.find((slot) => slot.offset_min === pref.offset_min)?.label ?? null
  const mailEnabled = mailQuery.data?.enabled ?? false
  const retention = retentionQuery.data?.days ?? 90

  const mailMutation = useMutation({
    mutationFn: setMailNotify,
    onSuccess: (data) => {
      queryClient.setQueryData(ME_KEYS.mailNotify, data)
      setError(null)
      notifySuccess(data.enabled ? '已开启失败邮件' : '已关闭失败邮件')
    },
    onError: () => setError('邮件偏好没有保存，请稍后再试。'),
  })

  const retentionMutation = useMutation({
    mutationFn: setPhotoRetention,
    onSuccess: (data) => {
      queryClient.setQueryData(ME_KEYS.photoRetention, data)
      setError(null)
      notifySuccess('照片保留期已设为 ' + data.days + ' 天')
    },
    onError: () => setError('照片保留期没有保存，请稍后再试。'),
  })

  const deleteMutation = useMutation({
    mutationFn: (password: string) => deleteMyAccount({ current_password: password }),
    onSuccess: () => {
      clearSessionExpired()
      queryClient.clear()
      void navigate({ to: '/login', replace: true })
    },
    onError: (err) => setDeleteError(deleteErrorText(err)),
  })

  const logoutMutation = useMutation({
    mutationFn: logout,
    onSuccess: () => {
      clearSessionExpired()
      queryClient.clear()
      void navigate({ to: '/login', replace: true })
    },
    onError: () => setError('退出失败，请检查网络后再试。'),
  })

  if (accountsQuery.isPending) {
    return (
      <div className="un-me">
        <span className="un-skeleton un-me__skeleton" />
      </div>
    )
  }

  if (accountsQuery.isError) {
    return (
      <div className="un-me">
        <header className="un-me__head">
          <h1 className="un-me__title">我的</h1>
          <p className="un-me__lede">签到档案、时间偏好与账号安全都收在这里。</p>
        </header>
        <p className="un-me__error" role="alert">
          账号信息没有加载成功，请检查网络后再试。
        </p>
        <button
          type="button"
          className="un-btn un-btn--secondary"
          onClick={() => void accountsQuery.refetch()}
        >
          重新加载
        </button>
      </div>
    )
  }

  return (
    <div className="un-me">
      <header className="un-me__head">
        <h1 className="un-me__title">我的</h1>
        <p className="un-me__lede">签到档案、时间偏好与账号安全都收在这里。</p>
      </header>

      {error ? (
        <p className="un-me__error" role="alert">
          {error}
        </p>
      ) : null}

      {live.length === 0 && items.length > 0 ? (
        <p className="un-alert un-alert--warning" role="status">
          签到账号已删除，7 天内可以恢复。
          <Link to="/app/me/account" className="un-link">
            去恢复
          </Link>
        </p>
      ) : null}

      <div className="un-me__grid">
        <div className="un-me__primary">
          <section className="un-profile un-shared-identity" aria-labelledby="un-profile-title">
            <div className="un-profile__head">
              <span className="un-avatar un-avatar--xl" aria-hidden="true">
                {photoUrl ? (
                  <img src={photoUrl} alt="" className="un-avatar__img" />
                ) : (
                  <span className="un-serif">{monogram(account?.name ?? '')}</span>
                )}
              </span>
              <div className="un-profile__names">
                <p className="un-eyebrow">签到账号</p>
                <h2 id="un-profile-title" className="un-profile__name">
                  {account
                    ? displayName(account.name, account.phone_masked)
                    : items.length > 0
                      ? '签到账号已删除'
                      : '还没有账号'}
                </h2>
                <p className="un-profile__sub">
                  {account
                    ? schoolName + ' · ' + account.phone_masked
                    : items.length > 0
                      ? '7 天内可以恢复，恢复后继续自动签到'
                      : '绑定易班账号后开始自动签到'}
                </p>
              </div>
              {account ? (
                <span className={cx('un-chip', 'un-chip--' + chipTone(userStatusTone(account.status, false)))}>
                  {userStatusLabel(account.status, false)}
                </span>
              ) : null}
            </div>

            {account ? (
              <dl className="un-profile__facts">
                <div>
                  <dt>下一次</dt>
                  <dd>{nextRunText(pref)}</dd>
                </div>
                <div>
                  <dt>时间窗口</dt>
                  <dd>{pref?.window ? pref.window.start + '–' + pref.window.end : '未设置'}</dd>
                </div>
                <div>
                  <dt>已选时间</dt>
                  <dd>{chosenSlot ?? '自动分配'}</dd>
                </div>
              </dl>
            ) : null}

            <div className="un-profile__actions">
              <Link
                to={account ? '/app/me/account' : '/app/me/account/new'}
                className="un-btn un-btn--primary"
              >
                {account ? '管理账号' : '新建账号'}
              </Link>
            </div>
          </section>

          <section className="un-me__section" aria-labelledby="un-me-stats">
            <div className="un-me__section-head">
              <h2 id="un-me-stats" className="un-me__section-title">近 30 天</h2>
              <p className="un-me__section-note">只统计已有任务的日期。</p>
            </div>
            <dl className="un-stats">
              <div className="un-stats__item">
                <dt>成功</dt>
                <dd>{calendarQuery.isError ? '—' : successDays}</dd>
                <span>天</span>
              </div>
              <div className="un-stats__item">
                <dt>连续</dt>
                <dd>{calendarQuery.isError ? '—' : streak}</dd>
                <span>天</span>
              </div>
              <div className="un-stats__item">
                <dt>失败</dt>
                <dd>{calendarQuery.isError ? '—' : failedDays}</dd>
                <span>天</span>
              </div>
            </dl>
            <div className="un-me__week-block">
              <div
                className="un-me__week"
                role="group"
                aria-label={
                  calendarQuery.isError
                    ? '近 30 天统计暂时不可用'
                    : `近 30 天：${successDays} 天全部成功，${failedDays} 天有失败，当前连续 ${streak} 天`
                }
              >
                {days.map((day) => {
                  const outcome =
                    day.total === 0
                      ? '没有任务'
                      : day.failed > 0
                        ? '有失败'
                        : day.success > 0
                          ? '成功'
                          : day.unknown > 0
                            ? '待确认'
                            : '未执行'
                  return (
                    <span
                      key={day.day}
                      className={cx(
                        'un-me__week-mark',
                        day.success > 0 && 'is-success',
                        day.failed > 0 && 'is-danger',
                        day.success === 0 && day.failed === 0 && day.unknown > 0 && 'is-pending',
                        day.total === 0 && 'is-empty',
                      )}
                      role="img"
                      aria-label={`${weekdayLabel(day.day)} ${day.day}：${outcome}`}
                      title={weekdayLabel(day.day) + ' ' + day.day}
                    />
                  )
                })}
              </div>
              {days.length > 0 ? (
                <div className="un-me__week-range" aria-hidden="true">
                  <span>{days[0].day.slice(5)}</span>
                  <span>今天 {today.slice(5)}</span>
                </div>
              ) : null}
            </div>
          </section>

          <section className="un-me__section" aria-labelledby="un-me-prefs">
            <div className="un-me__section-head">
              <h2 id="un-me-prefs" className="un-me__section-title">偏好</h2>
              <p className="un-me__section-note">改动即时保存。</p>
            </div>

            <div className="un-pref">
              <div className="un-pref__copy">
                <p className="un-pref__label">签到时间</p>
                <p className="un-pref__hint">
                  {timePrefQuery.isError
                    ? '时间偏好暂时读不到，请稍后刷新。'
                    : timePrefQuery.isPending
                      ? '正在读取时间偏好…'
                      : pref?.window
                        ? '每天 ' + pref.window.start + '–' + pref.window.end + ' 之间自动执行'
                        : '还没有设置时间窗口'}
                </p>
              </div>
              <Link to="/app/me/time" className="un-pref__action">
                调整 <span aria-hidden="true">→</span>
              </Link>
            </div>

            <div className="un-pref">
              <div className="un-pref__copy">
                <p className="un-pref__label">失败邮件</p>
                <p className="un-pref__hint">签到没有成功时，发一封说明到登录邮箱。</p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={mailEnabled}
                aria-label="失败邮件"
                className="un-switch"
                disabled={mailMutation.isPending || mailQuery.isPending || mailQuery.isError}
                onClick={() => mailMutation.mutate(!mailEnabled)}
              />
            </div>

            <div className="un-pref">
              <div className="un-pref__copy">
                <p className="un-pref__label">照片保留</p>
                <p className="un-pref__hint">现场照片只用于签到复核，过期自动删除。</p>
              </div>
              <select
                className="un-select un-pref__select"
                aria-label="照片保留期"
                value={retention}
                disabled={retentionMutation.isPending || retentionQuery.isPending || retentionQuery.isError}
                onChange={(event) => {
                  const next = Number(event.target.value) as (typeof RETENTION_OPTIONS)[number]
                  retentionMutation.mutate(next)
                }}
              >
                {RETENTION_OPTIONS.map((value) => (
                  <option key={value} value={value}>
                    {value} 天
                  </option>
                ))}
              </select>
            </div>
          </section>
        </div>

        <aside className="un-me__aside">
          <section className="un-security" aria-labelledby="un-me-security">
            <h2 id="un-me-security" className="un-security__title">账号与安全</h2>
            {me.data?.email ? (
              <p className="un-security__account">
                <span>当前登录</span>
                <span>{me.data.email}</span>
              </p>
            ) : null}
            <Link to="/app/me/password" className="un-security__item">
              <span>
                <span className="un-security__label">登录密码</span>
                <span className="un-security__hint">定期更换，不与易班口令相同</span>
              </span>
              <span aria-hidden="true">→</span>
            </Link>
            <Link to="/app/me/account" className="un-security__item">
              <span>
                <span className="un-security__label">易班口令</span>
                <span className="un-security__hint">在账号页更新或验证</span>
              </span>
              <span aria-hidden="true">→</span>
            </Link>
            <button
              type="button"
              className="un-security__item un-security__item--button"
              disabled={logoutMutation.isPending}
              onClick={() => logoutMutation.mutate()}
            >
              <span>
                <span className="un-security__label">退出登录</span>
                <span className="un-security__hint">只退出这台设备</span>
              </span>
              <span aria-hidden="true">→</span>
            </button>
            <button
              type="button"
              className="un-security__item un-security__item--button un-security__item--danger"
              onClick={(event) => {
                setDeleteReturnFocus(event.currentTarget)
                setDeleteError(null)
                setDeleteOpen(true)
              }}
            >
              <span>
                <span className="un-security__label">删除登录账号</span>
                <span className="un-security__hint">7 天内可用邮箱与口令恢复</span>
              </span>
              <span aria-hidden="true">→</span>
            </button>
          </section>

          <p className="un-me__aside-note">
            管理端看不到你的易班口令或完整手机号；现场照片只对本人和管理员可见。
          </p>

          <ReauthDialog
            open={deleteOpen}
            onOpenChange={(open) => {
              setDeleteOpen(open)
              if (!open) setDeleteError(null)
            }}
            title="删除登录账号"
            description="会退出登录并删除账号与签到数据；7 天内可用邮箱与口令恢复。需要输入本站登录口令。"
            confirmLabel="删除登录账号"
            danger
            pending={deleteMutation.isPending}
            error={deleteError}
            returnFocusTo={deleteReturnFocus}
            onConfirm={(password) => {
              setDeleteError(null)
              deleteMutation.mutate(password)
            }}
          />
        </aside>
      </div>
    </div>
  )
}

function monogram(name: string): string {
  const first = Array.from(name.trim())[0]
  return first ?? '我'
}
