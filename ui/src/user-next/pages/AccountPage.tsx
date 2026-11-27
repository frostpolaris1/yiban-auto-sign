/**
 * **功能**
 * 我的账号：身份、资料、设备、照片与控制。
 *
 * **归属**
 * user-next 页面层。取数与动作复用 features/me 的查询口径。
 *
 * **口径**
 * 一人一号；没有账号时给创建入口，不伪造控制台。
 * 改手机号/口令/机型需要本站口令；只改备注名不需要。
 * 删除是软删除，7 天内可恢复；恢复与刷新设备同样要二次口令。
 */

import { useRef, useState } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Modal } from '@/components/ui/Modal'
import { notifySuccess } from '@/components/ui/toast'
import {
  getAccountLocation,
  getPersonName,
  pauseAccount,
  refreshDevice,
  selfDeleteAccount,
  selfRestoreAccount,
  updateAccount,
  verifyAccount,
} from '@/lib/api/account'
import type { Account } from '@/lib/api/account'
import { ApiError } from '@/lib/api/client'
import { formatClock } from '@/lib/format'
import {
  deletePhoto,
  listAccountPhotos,
  photoBytesPath,
  uploadPhoto,
} from '@/lib/api/photo'
import type { Photo } from '@/lib/api/photo'
import {
  displayName,
  userStatusLabel,
  userStatusTone,
} from '@/lib/account-display'
import {
  calendarDayTone,
  nextRunText,
  photoUploadErrorText,
  todayKey,
  type CalendarDayTone,
  weekdayLabel,
} from '@/features/me/display'
import {
  ME_KEYS,
  useLatestPhoto,
  useMyAccounts,
  useSchools,
  useSignCalendar,
  useSignStatus,
  useTimePref,
} from '@/features/me/queries'

const MINI_WEEK_STATE: Record<CalendarDayTone, string> = {
  success: 'success',
  danger: 'failed',
  pending: 'pending',
  warning: 'skipped',
  none: 'empty',
}

type EditKind = 'remark' | 'credentials'
type ReauthKind = 'delete' | 'restore' | 'refresh'

function daysAgo(day: string, amount: number): string {
  const date = new Date(day + 'T00:00:00Z')
  date.setUTCDate(date.getUTCDate() + amount)
  return date.toISOString().slice(0, 10)
}

function authErrorText(error: unknown): string {
  if (error instanceof Error) {
    if (error.message === 'nothing to update') return '没有需要保存的修改。'
    if (error.message === 'missing current password') return '请输入本站登录口令。'
  }
  if (error instanceof ApiError) {
    if (error.status === 400) return '填写内容不合法，或缺少本站登录口令。'
    if (error.status === 403) return '口令不正确，或当前状态不允许这个操作。'
    if (error.status === 409) return '记录已被他人修改，请刷新后重试。'
    if (error.status === 429) return '尝试过多，请稍后再试。'
    if (error.status === 502) return '平台暂时不可用，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    return '操作失败，请稍后重试。'
  }
  return '网络异常，请稍后重试。'
}

function verifyText(result: {
  ok: boolean
  code: string
  identity?: { university_name: string }
}): string {
  if (result.ok) {
    return result.identity
      ? '凭据有效 · ' + result.identity.university_name
      : '凭据有效'
  }
  switch (result.code) {
    case 'credential_invalid':
      return '易班账号或口令失效，请更新后再试。'
    case 'challenge':
      return '平台要求验证码或人工验证，请按平台提示处理。'
    case 'blocked':
      return '触发平台限制，暂时无法验证。'
    case 'device_unbound':
      return '设备未绑定，请刷新设备后再试。'
    case 'transient':
      return '平台暂时不可用，请稍后再试。'
    case 'rejected':
      return '平台拒绝了本次验证。'
    default:
      return '未知结果，请稍后再试。'
  }
}

function monogram(account: Account): string {
  const text = account.name.trim() || account.phone_masked.trim() || '易'
  return text.slice(0, 1)
}

function chipClass(tone: string): string {
  if (tone === 'success' || tone === 'danger' || tone === 'warning') {
    return 'un-chip un-chip--' + tone
  }
  return 'un-chip un-chip--blue'
}

export function AccountPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const accountsQuery = useMyAccounts()
  const schoolsQuery = useSchools()
  const accounts = accountsQuery.data?.items ?? []
  const live = accounts.find((item) => item.deleted_at === null) ?? null
  const deleted =
    live === null ? (accounts.find((item) => item.deleted_at !== null) ?? null) : null
  const account = live ?? deleted
  const isDeleted = account?.deleted_at !== null && account?.deleted_at !== undefined

  const [editKind, setEditKind] = useState<EditKind | null>(null)
  const [reauthKind, setReauthKind] = useState<ReauthKind | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [reauthError, setReauthError] = useState<string | null>(null)
  const [verifyOpen, setVerifyOpen] = useState(false)
  const [personOpen, setPersonOpen] = useState(false)
  const [personName, setPersonName] = useState<string | null>(null)
  const [deviceMasked, setDeviceMasked] = useState<string | null>(null)
  const [photoBusy, setPhotoBusy] = useState(false)
  const [photoError, setPhotoError] = useState<string | null>(null)
  const [photoToDelete, setPhotoToDelete] = useState<Photo | null>(null)
  const triggerRef = useRef<HTMLElement | null>(null)
  const uploadRef = useRef<HTMLInputElement | null>(null)

  const [editName, setEditName] = useState('')
  const [editPhone, setEditPhone] = useState('')
  const [editPassword, setEditPassword] = useState('')
  const [editModel, setEditModel] = useState('')
  const [editCurrent, setEditCurrent] = useState('')
  const [editLocalError, setEditLocalError] = useState<string | null>(null)
  const [reauthPassword, setReauthPassword] = useState('')

  const photosQuery = useQuery({
    queryKey: ME_KEYS.photos(account?.id ?? 'none', 20),
    queryFn: () => listAccountPhotos(account!.id, { limit: 20 }),
    enabled: account !== null,
    staleTime: 0,
    retry: false,
  })
  const locationQuery = useQuery({
    queryKey: ['account-location', account?.id ?? 'none'],
    queryFn: () => getAccountLocation(account!.id),
    enabled: account !== null,
    staleTime: 0,
    retry: false,
  })
  const latestPhoto = useLatestPhoto(account?.id ?? null)

  const statusQuery = useSignStatus()
  const prefQuery = useTimePref()
  const today = todayKey(new Date())
  const calendarQuery = useSignCalendar(
    daysAgo(today, -6),
    today,
    account !== null && account.deleted_at === null,
  )
  const schools = schoolsQuery.data ?? []
  const schoolName = account
    ? (schools.find((item) => item.id === account.school_id)?.name ?? '')
    : ''
  const photos = photosQuery.data?.items ?? []
  const todayItem = account && statusQuery.data
    ? (statusQuery.data.items.find((item) => item.account_id === account.id) ?? null)
    : null
  const calendarDays = calendarQuery.data?.days ?? []
  const calendarToday = calendarQuery.data?.today ?? today

  let todayTone = 'quiet'
  let todayLabel = '今天暂无记录'
  if (account && account.status !== 'active') {
    todayLabel = userStatusLabel(account.status, account.deleted_at !== null)
  } else if (todayItem) {
    if (todayItem.state === 'success') {
      todayTone = 'success'
      todayLabel = todayItem.run_at ? '今天已签到 · ' + formatClock(todayItem.run_at) : '今天已签到'
    } else if (todayItem.state === 'failed') {
      todayTone = 'failed'
      todayLabel = '今天失败，等待重试'
    } else if (todayItem.state === 'skipped') {
      todayTone = 'warning'
      todayLabel = '今天未执行'
    } else if (todayItem.state === 'running') {
      todayLabel = '今天进行中'
    } else if (todayItem.state === 'pending') {
      todayTone = 'info'
      todayLabel = '今天排队中'
    }
  }

  const invalidateAccount = () => {
    void queryClient.invalidateQueries({ queryKey: ME_KEYS.accounts })
    void queryClient.invalidateQueries({ queryKey: ME_KEYS.signStatus })
    void queryClient.invalidateQueries({ queryKey: ME_KEYS.diagnostics })
  }

  const closeEdit = () => {
    setEditKind(null)
    setEditLocalError(null)
    setActionError(null)
    setEditPassword('')
    setEditCurrent('')
  }

  const openEdit = (kind: EditKind) => {
    if (!account) return
    triggerRef.current = document.activeElement as HTMLElement | null
    setEditName(account.name)
    setEditPhone('')
    setEditPassword('')
    setEditModel(account.phone_model ?? '')
    setEditCurrent('')
    setEditLocalError(null)
    setActionError(null)
    setEditKind(kind)
  }

  const closeReauth = () => {
    setReauthKind(null)
    setReauthError(null)
    setReauthPassword('')
  }

  const openReauth = (kind: ReauthKind) => {
    triggerRef.current = document.activeElement as HTMLElement | null
    setReauthPassword('')
    setReauthError(null)
    setActionError(null)
    setReauthKind(kind)
  }

  const updateMutation = useMutation({
    mutationFn: ({ kind }: { kind: EditKind }) => {
      if (!account) throw new Error('no account')
      const body: Record<string, string> = {
        expected_updated_at: account.updated_at,
      }
      const trimmedName = editName.trim()
      const nameChanged = trimmedName !== account.name
      if (kind === 'remark') {
        if (!nameChanged) throw new Error('nothing to update')
        body.name = trimmedName
      } else {
        if (nameChanged) body.name = trimmedName
        if (editPhone.trim() !== '') body.phone = editPhone.trim()
        if (editPassword !== '') body.password = editPassword
        const nextModel = editModel.trim()
        if (nextModel !== (account.phone_model ?? '')) body.phone_model = nextModel
        if (
          body.name === undefined &&
          !body.phone &&
          !body.password &&
          body.phone_model === undefined
        ) {
          throw new Error('nothing to update')
        }
        const needsAuth =
          body.phone !== undefined ||
          body.password !== undefined ||
          body.phone_model !== undefined
        if (needsAuth) {
          if (editCurrent === '') throw new Error('missing current password')
          body.current_password = editCurrent
        }
      }
      return updateAccount(account.id, body)
    },
    onSuccess: (updated) => {
      setActionError(null)
      setEditKind(null)
      notifySuccess(updated.status === 'pending' ? '已保存，等待重新审核' : '已保存')
      invalidateAccount()
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setEditKind(null)
        setActionError('记录已被他人修改，已刷新；请重新打开编辑。')
        invalidateAccount()
        return
      }
      setActionError(authErrorText(error))
    },
  })

  const pauseMutation = useMutation({
    mutationFn: (paused: boolean) => pauseAccount(account!.id, { paused }),
    onSuccess: (_data, paused) => {
      setActionError(null)
      notifySuccess(paused ? '已暂停签到' : '已恢复签到')
      invalidateAccount()
    },
    onError: (error) => setActionError(authErrorText(error)),
  })

  const reauthMutation = useMutation({
    mutationFn: async ({ kind, password }: { kind: ReauthKind; password: string }) => {
      if (!account) throw new Error('no account')
      if (kind === 'delete') return selfDeleteAccount(account.id, password)
      if (kind === 'restore') return selfRestoreAccount(account.id, password)
      return refreshDevice(account.id, password)
    },
    onSuccess: (result, variables) => {
      setReauthError(null)
      setReauthKind(null)
      if (variables.kind === 'delete') {
        notifySuccess('已删除，7 天内可恢复')
        invalidateAccount()
        void navigate({ to: '/app' })
        return
      }
      if (variables.kind === 'restore') {
        notifySuccess('已恢复签到账号')
        invalidateAccount()
        return
      }
      const device = result as { device_code_masked?: string }
      setDeviceMasked(device.device_code_masked ?? null)
      notifySuccess('设备已刷新')
      invalidateAccount()
    },
    onError: (error) => setReauthError(authErrorText(error)),
  })

  const verifyMutation = useMutation({
    mutationFn: () => verifyAccount(account!.id),
    onSuccess: () => setVerifyOpen(true),
    onError: (error) => setActionError(authErrorText(error)),
  })

  const personMutation = useMutation({
    mutationFn: () => getPersonName(account!.id),
    onSuccess: (result) => {
      setPersonName(result.person_name)
      setPersonOpen(true)
    },
    onError: (error) => setActionError(authErrorText(error)),
  })

  const refreshPhotos = () => {
    if (!account) return
    void queryClient.invalidateQueries({ queryKey: ME_KEYS.photosBase(account.id) })
  }

  const onUpload = async (file: File) => {
    setPhotoError(null)
    setPhotoBusy(true)
    try {
      await uploadPhoto(file)
      refreshPhotos()
      notifySuccess('照片已上传')
    } catch (error) {
      setPhotoError(photoUploadErrorText(error))
    } finally {
      setPhotoBusy(false)
      if (uploadRef.current) uploadRef.current.value = ''
    }
  }

  const onDeletePhoto = async (photo: Photo) => {
    setPhotoError(null)
    try {
      await deletePhoto(photo.id)
      refreshPhotos()
      notifySuccess('照片已删除')
    } catch (error) {
      setPhotoError(authErrorText(error))
    }
  }

  if (accountsQuery.isPending) {
    return (
      <div className="un-account" aria-busy="true">
        <div className="un-skeleton" style={{ width: 160, height: 36 }} />
        <div className="un-skeleton" style={{ width: '100%', height: 240, marginTop: 24 }} />
      </div>
    )
  }

  if (accountsQuery.isError) {
    return (
      <div className="un-account">
        <header className="un-account__head">
          <Link className="un-page-back" to="/app/me">
            <span aria-hidden="true">←</span> 返回我的
          </Link>
          <h1 className="un-account__title">签到账号</h1>
        </header>
        <div className="un-alert un-alert--danger" role="alert">
          账号读取失败。请检查网络后重试；这不是「没有账号」。
        </div>
        <button
          type="button"
          className="un-btn un-btn--secondary"
          style={{ marginTop: 16 }}
          onClick={() => void accountsQuery.refetch()}
        >
          重试
        </button>
      </div>
    )
  }

  if (!account) {
    return (
      <div className="un-account">
        <header className="un-account__head">
          <Link className="un-page-back" to="/app/me">
            <span aria-hidden="true">←</span> 返回我的
          </Link>
          <h1 className="un-account__title">签到账号</h1>
        </header>
        <section className="un-account-empty">
          <span className="un-account-empty__mark" aria-hidden="true">未</span>
          <div>
            <h2 className="un-account-empty__title">还没有签到账号</h2>
            <p className="un-account-empty__text">
              绑定易班后，每天在窗口内自动签到。只需要手机号和一次易班口令。
            </p>
            <div className="un-account__actions">
              <Link className="un-btn un-btn--primary" to="/app/me/account/new">
                连接易班账号
              </Link>
              <Link className="un-btn un-btn--ghost" to="/app">
                回到今天
              </Link>
            </div>
          </div>
        </section>
      </div>
    )
  }

  const isPaused = account.status === 'paused_user'
  const pausedByAdmin = account.status === 'paused_admin'
  const canPause = !isDeleted && (account.status === 'active' || isPaused)
  const avatarPhoto = latestPhoto.data?.items[0] ?? null
  const pref = prefQuery.data

  return (
    <div className="un-account">
      <header className="un-account__head">
        <Link className="un-page-back" to="/app/me">
          <span aria-hidden="true">←</span> 返回我的
        </Link>
        <h1 className="un-account__title">签到账号</h1>
      </header>

      <section className="un-account-hero" aria-label="账号身份">
        <span className="un-avatar un-avatar--xl un-account-hero__avatar" aria-hidden="true">
          {avatarPhoto ? (
            <img src={photoBytesPath(avatarPhoto.id)} alt="" className="un-avatar__img" />
          ) : (
            monogram(account)
          )}
        </span>
        <div className="un-account-hero__text">
          <h2 className="un-account-hero__name">
            {displayName(account.name, account.phone_masked)}
          </h2>
          <p className="un-account-hero__meta">
            {schoolName ? schoolName + ' · ' : ''}
            {account.phone_masked}
          </p>
          <p className="un-account-hero__dates">
            创建 {account.created_at.slice(0, 10)}
            <span aria-hidden="true"> · </span>
            更新 {account.updated_at.slice(0, 10)}
            {account.reviewed_at ? (
              <>
                <span aria-hidden="true"> · </span>
                审核 {account.reviewed_at.slice(0, 10)}
              </>
            ) : null}
          </p>
        </div>
        <div className="un-account-hero__side">
          <span className={chipClass(userStatusTone(account.status, isDeleted))}>
            <i className="un-chip__dot" aria-hidden="true" />
            {userStatusLabel(account.status, isDeleted)}
          </span>
          {isDeleted ? (
            <p className="un-account-hero__note">7 天内可恢复</p>
          ) : null}
        </div>
        {account.reject_reason ? (
          <p className="un-account-hero__reject">
            <strong>驳回理由</strong>
            {account.reject_reason}
          </p>
        ) : null}
      </section>

      {actionError ? (
        <div className="un-alert un-alert--danger un-account__alert" role="alert">
          {actionError}
        </div>
      ) : null}

      <div className="un-account__grid">
        <div className="un-account__main">
          <section className="un-section">
            <div className="un-section__head">
              <h2 className="un-section__title">资料</h2>
              <span className="un-section__hint">备注名不影响审核</span>
            </div>
            <div className="un-rows">
              <button
                type="button"
                className="un-row"
                disabled={isDeleted}
                onClick={() => openEdit('remark')}
              >
                <span className="un-row__label">备注名</span>
                <span className="un-row__value">
                  {account.name || '未设置'}
                </span>
                <span className="un-row__sub">只影响你自己的界面</span>
                <span className="un-row__chevron" aria-hidden="true">›</span>
              </button>
              <button
                type="button"
                className="un-row"
                disabled={isDeleted}
                onClick={() => openEdit('credentials')}
              >
                <span className="un-row__label">易班资料</span>
                <span className="un-row__value">{account.phone_masked}</span>
                <span className="un-row__sub">手机号、口令与机型；保存后可能重审</span>
                <span className="un-row__chevron" aria-hidden="true">›</span>
              </button>
            </div>
          </section>

          <section className="un-section">
            <div className="un-section__head">
              <h2 className="un-section__title">设备</h2>
              <span className="un-section__hint">只读</span>
            </div>
            <dl className="un-pairs">
              <div>
                <dt>机型</dt>
                <dd>{account.phone_model || '未知'}</dd>
              </div>
              <div>
                <dt>设备码</dt>
                <dd>{deviceMasked ?? '刷新后读取'}</dd>
              </div>
            </dl>
            <button
              type="button"
              className="un-row"
              disabled={isDeleted}
              onClick={() => openReauth('refresh')}
            >
              <span className="un-row__label">刷新设备</span>
              <span className="un-row__value">从易班重新读取</span>
              <span className="un-row__sub">不修改账号资料；需要本站口令</span>
              <span className="un-row__chevron" aria-hidden="true">›</span>
            </button>
          </section>

          <section className="un-section">
            <div className="un-section__head">
              <h2 className="un-section__title">现场照片</h2>
              <span className="un-section__hint">
                {photos.length} 张 · 审核可见
              </span>
            </div>
            <input
              ref={uploadRef}
              type="file"
              accept="image/png,image/jpeg"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void onUpload(file)
              }}
            />
            {photosQuery.isError ? (
              <div className="un-photos__error">
                <p>照片读取失败。</p>
                <button
                  type="button"
                  className="un-linkbtn"
                  onClick={() => void photosQuery.refetch()}
                >
                  重试
                </button>
              </div>
            ) : photos.length === 0 ? (
              <button
                type="button"
                className="un-photos__empty"
                disabled={photoBusy || isDeleted}
                onClick={() => uploadRef.current?.click()}
              >
                <span className="un-photos__empty-mark" aria-hidden="true">+</span>
                <span>添加第一张现场照片</span>
                <small>PNG / JPEG · 仅用于签到审核</small>
              </button>
            ) : (
              <div className="un-photos">
                {photos.map((photo) => (
                  <figure className="un-photos__tile" key={photo.id}>
                    <img src={photoBytesPath(photo.id)} alt="现场照片" />
                    <button
                      type="button"
                      className="un-photos__remove"
                      aria-label="删除照片"
                      onClick={() => {
                        triggerRef.current = document.activeElement as HTMLElement | null
                        setPhotoToDelete(photo)
                      }}
                    >
                      删除
                    </button>
                  </figure>
                ))}
                <button
                  type="button"
                  className="un-photos__add"
                  disabled={photoBusy || isDeleted}
                  onClick={() => uploadRef.current?.click()}
                >
                  {photoBusy ? '上传中…' : '添加照片'}
                </button>
              </div>
            )}
            {photoError ? (
              <p className="un-field__error" role="alert">{photoError}</p>
            ) : null}
            <p className="un-section__foot">
              照片仅用于签到审核；保留期在「我的」里调整。
            </p>
          </section>
        </div>

        <aside className="un-account__rail">
          <section className="un-rail-card un-account-summary">
            <div className="un-section__head">
              <h2 className="un-section__title">签到概览</h2>
              <Link className="un-linkbtn" to="/app/history">记录</Link>
            </div>
            <p className={'un-account-summary__state is-' + todayTone}>
              {todayLabel}
            </p>
            {calendarDays.length > 0 ? (
              <div className="un-miniweek" aria-label="近 7 天签到">
                {calendarDays.map((day) => {
                  const state = MINI_WEEK_STATE[calendarDayTone(day)]
                  return (
                    <span
                      key={day.day}
                      className={
                        'un-miniweek__day is-' + state +
                        (day.day === calendarToday ? ' is-today' : '')
                      }
                      title={day.day}
                    >
                      <small>{weekdayLabel(day.day)}</small>
                      <i aria-hidden="true" />
                    </span>
                  )
                })}
              </div>
            ) : null}
            <dl className="un-account-summary__facts">
              <div>
                <dt>下一次</dt>
                <dd>{nextRunText(pref)}</dd>
              </div>
              <div>
                <dt>窗口</dt>
                <dd>{pref?.window ? pref.window.start + '–' + pref.window.end : '—'}</dd>
              </div>
            </dl>
          </section>

          <section className="un-section un-account__controls">
            <div className="un-section__head">
              <h2 className="un-section__title">控制</h2>
            </div>
            <div className="un-rows">
              <button
                type="button"
                className="un-row"
                disabled={!canPause || pausedByAdmin || pauseMutation.isPending}
                onClick={() => pauseMutation.mutate(!isPaused)}
              >
                <span className="un-row__label">{isPaused ? '恢复签到' : '暂停签到'}</span>
                <span className="un-row__value">
                  {pausedByAdmin ? '管理员暂停' : isPaused ? '已暂停' : '运行中'}
                </span>
                <span className="un-row__sub">
                  {pausedByAdmin
                    ? '管理员暂停，请联系管理员'
                    : isPaused
                      ? '恢复后从下一次开始'
                      : '暂停期间不签到'}
                </span>
                <span className="un-row__chevron" aria-hidden="true">›</span>
              </button>
              <button
                type="button"
                className="un-row"
                disabled={isDeleted || verifyMutation.isPending}
                onClick={() => {
                  triggerRef.current = document.activeElement as HTMLElement | null
                  setActionError(null)
                  verifyMutation.mutate()
                }}
              >
                <span className="un-row__label">验证凭据</span>
                <span className="un-row__value">
                  {verifyMutation.isPending ? '检查中' : '检查'}
                </span>
                <span className="un-row__sub">只读验证，不修改账号</span>
                <span className="un-row__chevron" aria-hidden="true">›</span>
              </button>
              <button
                type="button"
                className="un-row"
                disabled={isDeleted || personMutation.isPending}
                onClick={() => {
                  triggerRef.current = document.activeElement as HTMLElement | null
                  setActionError(null)
                  personMutation.mutate()
                }}
              >
                <span className="un-row__label">查看平台姓名</span>
                <span className="un-row__value">
                  {personMutation.isPending ? '读取中' : '读取'}
                </span>
                <span className="un-row__sub">按需读取，不保存</span>
                <span className="un-row__chevron" aria-hidden="true">›</span>
              </button>
              <div className="un-row un-row--static">
                <span className="un-row__label">签到地点</span>
                <span className="un-row__value">
                  {locationQuery.isError
                    ? '读取失败'
                    : locationQuery.data?.mode === 'fixed'
                      ? '固定位置'
                      : '自动定位'}
                </span>
                <span className="un-row__sub">
                  {locationQuery.isError
                    ? '地点读取失败，可稍后重试'
                    : locationQuery.data?.mode === 'fixed'
                      ? '位置由管理员设置'
                      : '按设备当前位置签到'}
                </span>
              </div>
              <button
                type="button"
                className="un-row un-row--danger"
                onClick={() => openReauth(isDeleted ? 'restore' : 'delete')}
              >
                <span className="un-row__label">{isDeleted ? '恢复签到账号' : '删除签到账号'}</span>
                <span className="un-row__value">{isDeleted ? '可恢复' : '需验证'}</span>
                <span className="un-row__sub">
                  {isDeleted ? '7 天内可恢复' : '停止签到，7 天内可恢复'}
                </span>
                <span className="un-row__chevron" aria-hidden="true">›</span>
              </button>
            </div>
          </section>
        </aside>
      </div>

      <Modal
        open={editKind !== null}
        onOpenChange={(open) => {
          if (!open) closeEdit()
        }}
        title={editKind === 'credentials' ? '修改账号资料' : '修改备注名'}
        description={
          editKind === 'credentials'
            ? '手机号、口令与机型属于凭据编辑；保存后可能需要重新审核。'
            : '备注名只用于显示，不会触发重新审核。'
        }
        className="un-dialog"
        wide={editKind === 'credentials'}
        scrollBody={editKind === 'credentials'}
        returnFocusTo={triggerRef.current}
        footer={
          <>
            <button type="button" className="un-btn un-btn--ghost" disabled={updateMutation.isPending} onClick={closeEdit}>
              取消
            </button>
            <button
              type="submit"
              form="un-account-edit"
              className="un-btn un-btn--primary"
              disabled={updateMutation.isPending}
            >
              {updateMutation.isPending ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        <form
          id="un-account-edit"
          className="un-form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            if (updateMutation.isPending || editKind === null) return
            const trimmedPhone = editPhone.trim()
            const nextModel = editModel.trim()
            const nameChanged = editName.trim() !== account.name
            if (editKind === 'credentials') {
              if (trimmedPhone !== '' && !/^1[0-9]{10}$/.test(trimmedPhone)) {
                setEditLocalError('手机号应为 11 位数字。')
                return
              }
              const needsAuth =
                trimmedPhone !== '' ||
                editPassword !== '' ||
                nextModel !== (account.phone_model ?? '')
              if (!needsAuth && !nameChanged) {
                setEditLocalError('没有需要保存的修改。')
                return
              }
              if (needsAuth && editCurrent === '') {
                setEditLocalError('请输入本站登录口令。')
                return
              }
            } else if (!nameChanged) {
              setEditLocalError('没有需要保存的修改。')
              return
            }
            setEditLocalError(null)
            updateMutation.mutate({ kind: editKind })
          }}
        >
          <div className="un-field">
            <label className="un-field__label" htmlFor="uapp-edit-name">备注名</label>
            <input
              id="uapp-edit-name"
              className="un-input"
              value={editName}
              maxLength={50}
              placeholder="留空显示为账号 + 手机尾号"
              onChange={(event) => setEditName(event.target.value)}
            />
          </div>
          {editKind === 'credentials' ? (
            <>
              <div className="un-field">
                <label className="un-field__label" htmlFor="uapp-edit-phone">新的易班手机号</label>
                <input
                  id="uapp-edit-phone"
                  className="un-input"
                  type="tel"
                  inputMode="numeric"
                  value={editPhone}
                  placeholder="留空表示不修改"
                  onChange={(event) => setEditPhone(event.target.value)}
                />
              </div>
              <div className="un-field">
                <label className="un-field__label" htmlFor="uapp-edit-password">新的易班登录口令</label>
                <input
                  id="uapp-edit-password"
                  className="un-input"
                  type="password"
                  autoComplete="new-password"
                  value={editPassword}
                  placeholder="留空表示不修改"
                  onChange={(event) => setEditPassword(event.target.value)}
                />
              </div>
              <div className="un-field">
                <label className="un-field__label" htmlFor="uapp-edit-model">机型</label>
                <input
                  id="uapp-edit-model"
                  className="un-input"
                  value={editModel}
                  maxLength={64}
                  onChange={(event) => setEditModel(event.target.value)}
                />
                <p className="un-field__hint">机型不是设备码；设备码只能通过刷新设备读取。</p>
              </div>
              <div className="un-field">
                <label className="un-field__label" htmlFor="uapp-edit-current">本站登录口令</label>
                <input
                  id="uapp-edit-current"
                  className="un-input"
                  type="password"
                  autoComplete="current-password"
                  value={editCurrent}
                  placeholder="修改凭据需要确认本人"
                  onChange={(event) => setEditCurrent(event.target.value)}
                />
              </div>
            </>
          ) : null}
          {editLocalError ? (
            <p className="un-field__error" role="alert">{editLocalError}</p>
          ) : null}
          {actionError ? (
            <p className="un-field__error" role="alert">{actionError}</p>
          ) : null}
        </form>
      </Modal>

      <Modal
        open={reauthKind !== null}
        onOpenChange={(open) => {
          if (!open) closeReauth()
        }}
        title={
          reauthKind === 'delete'
            ? '删除签到账号'
            : reauthKind === 'restore'
              ? '恢复签到账号'
              : '刷新设备'
        }
        description={
          reauthKind === 'delete'
            ? '停止签到并移出列表，7 天内可以恢复。需要输入本站登录口令。'
            : reauthKind === 'restore'
              ? '恢复后继续自动签到。需要输入本站登录口令。'
              : '从易班重新读取设备信息，不修改账号。需要输入本站登录口令。'
        }
        className="un-dialog"
        returnFocusTo={triggerRef.current}
        footer={
          <>
            <button type="button" className="un-btn un-btn--ghost" onClick={closeReauth}>
              取消
            </button>
            <button
              type="button"
              className={reauthKind === 'delete' ? 'un-btn un-btn--danger' : 'un-btn un-btn--primary'}
              disabled={reauthMutation.isPending}
              onClick={() => {
                if (reauthKind === null) return
                if (reauthPassword === '') {
                  setReauthError('请输入本站登录口令。')
                  return
                }
                setReauthError(null)
                reauthMutation.mutate({ kind: reauthKind, password: reauthPassword })
              }}
            >
              {reauthMutation.isPending
                ? '处理中…'
                : reauthKind === 'delete'
                  ? '删除签到账号'
                  : reauthKind === 'restore'
                    ? '恢复签到账号'
                    : '刷新设备'}
            </button>
          </>
        }
      >
        <div className="un-field">
          <label className="un-field__label" htmlFor="uapp-reauth-password">本站登录口令</label>
          <input
            id="uapp-reauth-password"
            className="un-input"
            type="password"
            autoComplete="current-password"
            value={reauthPassword}
            onChange={(event) => setReauthPassword(event.target.value)}
          />
        </div>
        {reauthError ? (
          <p className="un-field__error" role="alert">{reauthError}</p>
        ) : null}
      </Modal>

      <Modal
        open={verifyOpen}
        onOpenChange={setVerifyOpen}
        title="验证结果"
        className="un-dialog"
        returnFocusTo={triggerRef.current}
        footer={
          <button type="button" className="un-btn un-btn--secondary" onClick={() => setVerifyOpen(false)}>
            知道了
          </button>
        }
      >
        <p className="un-dialog__text">
          {verifyMutation.data ? verifyText(verifyMutation.data) : ''}
        </p>
      </Modal>

      <Modal
        open={personOpen}
        onOpenChange={setPersonOpen}
        title="平台姓名"
        description="只显示本次读取结果，不保存到账号。"
        className="un-dialog"
        returnFocusTo={triggerRef.current}
        footer={
          <button type="button" className="un-btn un-btn--secondary" onClick={() => setPersonOpen(false)}>
            关闭
          </button>
        }
      >
        <p className="un-dialog__person">
          {personName && personName.trim() !== '' ? personName : '平台没有返回姓名'}
        </p>
      </Modal>

      <Modal
        open={photoToDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPhotoToDelete(null)
        }}
        title="删除现场照片"
        description="照片用于签到审核，删除后不可恢复。"
        className="un-dialog"
        returnFocusTo={triggerRef.current}
        footer={
          <>
            <button type="button" className="un-btn un-btn--ghost" onClick={() => setPhotoToDelete(null)}>
              取消
            </button>
            <button
              type="button"
              className="un-btn un-btn--danger"
              onClick={() => {
                const target = photoToDelete
                setPhotoToDelete(null)
                if (target) void onDeletePhoto(target)
              }}
            >
              删除照片
            </button>
          </>
        }
      >
        <p className="un-dialog__text">确定删除这张现场照片吗？</p>
      </Modal>
    </div>
  )
}
