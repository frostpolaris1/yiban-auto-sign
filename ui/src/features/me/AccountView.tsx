/**
 * **功能**
 * 用户端账号详情：资料、设备、现场照片、控制与按需验证。
 *
 * **归属**
 * `features/me` 域容器：取数、动作与错误映射在此；壳与导航在外层。
 *
 * **口径**
 * 一人一号：只渲染当前账号，不提供列表。
 * 备注名不触发重审；手机/口令/机型属于凭据编辑，要二次口令并可能回到待审核。
 * 删除/恢复/设备刷新统一走 `ReauthDialog`；400/403/429 各有中文。
 * 平台姓名按需读取，只显示本次结果，不落库。
 * 地点只显示自动/固定，不做地图与坐标输入。
 */

import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useRouterState } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Icon } from '@/components/ui/Icon'
import { PasswordInput, TextInput } from '@/components/ui/Input'
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
import type { Account, AccountStatus } from '@/lib/api/account'
import { ApiError } from '@/lib/api/client'
import { listAccountPhotos, deletePhoto, photoBytesPath, uploadPhoto } from '@/lib/api/photo'
import type { Photo } from '@/lib/api/photo'
import { displayName, userStatusLabel, userStatusTone } from '@/lib/account-display'
import { AccountWizard } from './AccountWizard'
import { nextRunText, photoUploadErrorText, todayKey } from './display'
import {
  ME_KEYS,
  useMyAccounts,
  useSchools,
  useSignCalendar,
  useSignStatus,
  useTimePref,
} from './queries'
import { ReauthDialog } from './ReauthDialog'
import { WeekStrip } from './WeekStrip'

type EditKind = 'remark' | 'credentials'
type ReauthKind = 'delete' | 'restore' | 'refresh'

function addDays(day: string, amount: number): string {
  const date = new Date(`${day}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + amount)
  return date.toISOString().slice(0, 10)
}

function statusClass(status: AccountStatus, deleted: boolean): string {
  return `uapp-status uapp-status--${userStatusTone(status, deleted)}`
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

function verifyText(result: { ok: boolean; code: string; identity?: { university_name: string } }): string {
  if (result.ok) {
    return result.identity
      ? `凭据有效 · ${result.identity.university_name}`
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

function EditModal({
  open,
  kind,
  account,
  pending,
  error,
  returnFocusTo,
  onClose,
  onSubmit,
}: {
  open: boolean
  kind: EditKind
  account: Account
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onSubmit: (values: {
    name: string
    phone: string
    password: string
    phoneModel: string
    currentPassword: string
  }) => void
}) {
  const [name, setName] = useState(account.name)
  const [phone, setPhone] = useState('')
  const [password, setPassword] = useState('')
  const [phoneModel, setPhoneModel] = useState(account.phone_model ?? '')
  const [currentPassword, setCurrentPassword] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setName(account.name)
    setPhone('')
    setPassword('')
    setPhoneModel(account.phone_model ?? '')
    setCurrentPassword('')
    setLocalError(null)
  }, [open, account])

  const credentials = kind === 'credentials'

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title={credentials ? '修改账号资料' : '修改备注名'}
      returnFocusTo={returnFocusTo}
      description={
        credentials
          ? '手机号、易班口令与机型属于凭据编辑；保存后可能需要重新审核。'
          : '备注名只用于显示，不会触发重新审核。'
      }
      scrollBody={credentials}
      footer={
        <>
          <button
            type="button"
            className="uapp-button uapp-button--ghost"
            disabled={pending}
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="submit"
            form="uapp-account-edit"
            className="uapp-button uapp-button--primary"
            disabled={pending}
          >
            {pending ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <form
        id="uapp-account-edit"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          if (pending) return
          const trimmedPhone = phone.trim()
          const nextModel = phoneModel.trim()
          const nameChanged = name.trim() !== account.name
          if (credentials) {
            if (trimmedPhone !== '' && !/^1[0-9]{10}$/.test(trimmedPhone)) {
              setLocalError('手机号应为 11 位数字。')
              return
            }
            const needsAuth =
              trimmedPhone !== '' ||
              password !== '' ||
              nextModel !== (account.phone_model ?? '')
            if (!needsAuth && !nameChanged) {
              setLocalError('没有需要保存的修改。')
              return
            }
            if (needsAuth && currentPassword === '') {
              setLocalError('请输入本站登录口令。')
              return
            }
          } else if (!nameChanged) {
            setLocalError('没有需要保存的修改。')
            return
          }
          setLocalError(null)
          onSubmit({ name, phone, password, phoneModel, currentPassword })
        }}
      >
        <div className="uapp-field">
          <TextInput
            id="uapp-edit-name"
            label="备注名"
            value={name}
            maxLength={50}
            placeholder="留空显示为账号 + 手机尾号"
            onChange={(event) => setName(event.target.value)}
          />
        </div>

        {credentials ? (
          <>
            <div className="uapp-field">
              <TextInput
                id="uapp-edit-phone"
                label="新的易班手机号"
                type="tel"
                inputMode="numeric"
                value={phone}
                placeholder="留空表示不修改"
                onChange={(event) => setPhone(event.target.value)}
              />
            </div>
            <div className="uapp-field">
              <PasswordInput
                id="uapp-edit-password"
                label="新的易班登录口令"
                value={password}
                placeholder="留空表示不修改"
                autoComplete="new-password"
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
            <div className="uapp-field">
              <TextInput
                id="uapp-edit-model"
                label="机型"
                value={phoneModel}
                maxLength={64}
                hint="机型不是设备码；设备码只能通过刷新设备读取"
                onChange={(event) => setPhoneModel(event.target.value)}
              />
            </div>
            <div className="uapp-field">
              <PasswordInput
                id="uapp-edit-current"
                label="本站登录口令"
                value={currentPassword}
                placeholder="修改凭据需要确认本人"
                autoComplete="current-password"
                onChange={(event) => setCurrentPassword(event.target.value)}
              />
            </div>
          </>
        ) : null}

        {localError ? (
          <p className="uapp-field-error" role="alert">
            {localError}
          </p>
        ) : null}
        {error ? (
          <div className="uapp-alert-block">
            <Alert tone="danger">{error}</Alert>
          </div>
        ) : null}
      </form>
    </Modal>
  )
}

export function AccountView({ initialWizardOpen = false }: { initialWizardOpen?: boolean }) {
  const navigate = useNavigate()
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const queryClient = useQueryClient()
  const accountsQuery = useMyAccounts()
  const schoolsQuery = useSchools()

  // 新建向导以模态承载：账号页空态 CTA 与 /new 深链共用同一实例口径。
  const [wizardOpen, setWizardOpen] = useState(initialWizardOpen)
  const [wizardSession, setWizardSession] = useState(0)
  const [wizardReturnFocus, setWizardReturnFocus] = useState<HTMLElement | null>(null)
  const openWizard = (trigger: HTMLElement | null) => {
    setWizardReturnFocus(trigger)
    setWizardSession((session) => session + 1)
    setWizardOpen(true)
  }
  const closeWizard = (open: boolean) => {
    if (open) return
    setWizardOpen(false)
    // /new 深链关闭后归位到账号页，URL 与内容一致。
    if (pathname === '/app/account/new') {
      void navigate({ to: '/app/account', replace: true })
    }
  }

  const accounts = accountsQuery.data?.items ?? []
  const live = accounts.find((item) => item.deleted_at === null) ?? null
  const deleted = live === null ? (accounts.find((item) => item.deleted_at !== null) ?? null) : null
  const account = live ?? deleted

  const [editKind, setEditKind] = useState<EditKind | null>(null)
  const [reauthKind, setReauthKind] = useState<ReauthKind | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [reauthError, setReauthError] = useState<string | null>(null)
  const [verifyOpen, setVerifyOpen] = useState(false)
  const [personOpen, setPersonOpen] = useState(false)
  const [editReturnFocus, setEditReturnFocus] = useState<HTMLElement | null>(null)
  const [reauthReturnFocus, setReauthReturnFocus] = useState<HTMLElement | null>(null)
  const [verifyReturnFocus, setVerifyReturnFocus] = useState<HTMLElement | null>(null)
  const [personReturnFocus, setPersonReturnFocus] = useState<HTMLElement | null>(null)
  const [personName, setPersonName] = useState<string | null>(null)
  const [deviceMasked, setDeviceMasked] = useState<string | null>(null)
  const [photoBusy, setPhotoBusy] = useState(false)
  const [photoError, setPhotoError] = useState<string | null>(null)
  const [photoToDelete, setPhotoToDelete] = useState<Photo | null>(null)
  const uploadRef = useRef<HTMLInputElement | null>(null)

  const photosQuery = useQuery({
    queryKey: account ? ME_KEYS.photos(account.id) : ['account', 'none', 'photos'],
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

  const schools = schoolsQuery.data ?? []
  const schoolName = account
    ? (schools.find((item) => item.id === account.school_id)?.name ?? '')
    : ''
  const photos = photosQuery.data?.items ?? []

  // 概览卡：今日结果＋近 7 天＋下一次，全部来自现有端点，不新增后端。
  const statusQuery = useSignStatus()
  const prefQuery = useTimePref()
  const today = todayKey(new Date())
  const calendarQuery = useSignCalendar(
    addDays(today, -6),
    today,
    account !== null && account.deleted_at === null,
  )
  const todayItem =
    account && statusQuery.data
      ? (statusQuery.data.items.find((item) => item.account_id === account.id) ?? null)
      : null
  let todayMark: 'ok' | 'missed' | 'skipped' | 'pending' | 'none' = 'none'
  let todayText = '今天暂无记录'
  let todaySub = ''
  if (account && account.status !== 'active') {
    todayText = userStatusLabel(account.status, account.deleted_at !== null)
    todaySub = '账号状态优先于今日结果'
  } else if (todayItem) {
    if (todayItem.state === 'success') {
      todayMark = 'ok'
      todayText = '今天已成功'
      todaySub = todayItem.run_at ? `实际 ${todayItem.run_at.slice(11, 16)}` : ''
    } else if (todayItem.state === 'failed') {
      todayMark = 'missed'
      todayText = '今天失败'
      todaySub = '会自动重试；详见主页诊断'
    } else if (todayItem.state === 'skipped') {
      todayMark = 'skipped'
      todayText = '今天未执行'
    } else {
      todayMark = 'pending'
      todayText = '今天进行中'
    }
  }

  const invalidateAccount = () => {
    void queryClient.invalidateQueries({ queryKey: ME_KEYS.accounts })
    void queryClient.invalidateQueries({ queryKey: ME_KEYS.signStatus })
    void queryClient.invalidateQueries({ queryKey: ME_KEYS.diagnostics })
  }

  const closeReauth = () => {
    setReauthKind(null)
    setReauthError(null)
  }

  const updateMutation = useMutation({
    mutationFn: ({
      values,
      kind,
    }: {
      values: {
        name: string
        phone: string
        password: string
        phoneModel: string
        currentPassword: string
      }
      kind: EditKind
    }) => {
      if (!account) throw new Error('no account')
      const body: Record<string, string> = { expected_updated_at: account.updated_at }
      const trimmedName = values.name.trim()
      const nameChanged = trimmedName !== account.name
      if (kind === 'remark') {
        if (!nameChanged) throw new Error('nothing to update')
        body.name = trimmedName
      } else {
        if (nameChanged) body.name = trimmedName
        if (values.phone.trim() !== '') body.phone = values.phone.trim()
        if (values.password !== '') body.password = values.password
        const nextModel = values.phoneModel.trim()
        // 清空机型也是修改：显式发送空串，服务端按凭据编辑处理。
        if (nextModel !== (account.phone_model ?? '')) {
          body.phone_model = nextModel
        }
        if (
          body.name === undefined &&
          !body.phone &&
          !body.password &&
          body.phone_model === undefined
        ) {
          throw new Error('nothing to update')
        }
        // 契约只在 body 携带凭据字段时要求 current_password；只改备注不索要。
        const needsAuth =
          body.phone !== undefined || body.password !== undefined || body.phone_model !== undefined
        if (needsAuth) {
          if (values.currentPassword === '') throw new Error('missing current password')
          body.current_password = values.currentPassword
        }
      }
      return updateAccount(account.id, body)
    },
    onSuccess: (updated) => {
      setActionError(null)
      setEditKind(null)
      notifySuccess(
        updated.status === 'pending' ? '已保存，等待重新审核' : '已保存',
      )
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
    void queryClient.invalidateQueries({ queryKey: ME_KEYS.photos(account.id) })
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
      <div className="uapp-page uapp-page--form" aria-busy="true">
        <div className="uapp-skeleton" style={{ width: 120, height: 34 }} />
        <div className="uapp-skeleton" style={{ width: '100%', height: 200, marginTop: 20, borderRadius: 24 }} />
        <AccountWizard
          key={wizardSession}
          open={wizardOpen}
          onOpenChange={closeWizard}
          returnFocusTo={wizardReturnFocus}
        />
      </div>
    )
  }

  if (accountsQuery.isError) {
    return (
      <div className="uapp-page uapp-page--form">
        <h1 className="uapp-greeting">账号</h1>
        <section className="uapp-card">
          <Alert tone="danger" title="账号读取失败">
            请检查网络后重试；这不是「没有账号」。
          </Alert>
          <button
            type="button"
            className="uapp-button uapp-button--secondary"
            onClick={() => void accountsQuery.refetch()}
          >
            重试
          </button>
        </section>
        <AccountWizard
          key={wizardSession}
          open={wizardOpen}
          onOpenChange={closeWizard}
          returnFocusTo={wizardReturnFocus}
        />
      </div>
    )
  }

  if (!account) {
    return (
      <div className="uapp-page uapp-page--form">
        <h1 className="uapp-greeting">账号</h1>
        <section className="uapp-hero uapp-hero--info uapp-enter" aria-label="还没有账号">
          <div className="uapp-hero-identity">
            <div className="uapp-hero-photo">
              <Icon name="user" size={22} />
            </div>
            <div className="uapp-hero-identity-text">
              <h2 className="uapp-hero-name">还没有签到账号</h2>
              <p className="uapp-hero-meta">绑定易班后，每天按时帮你打卡。</p>
            </div>
          </div>
          <div className="uapp-hero-actions">
            <button
              type="button"
              className="uapp-button uapp-button--primary"
              onClick={(event) => openWizard(event.currentTarget)}
            >
              连接易班账号
            </button>
            <Link className="uapp-hero-link" to="/app">
              返回主页 ›
            </Link>
          </div>
        </section>
        <AccountWizard
          key={wizardSession}
          open={wizardOpen}
          onOpenChange={closeWizard}
          returnFocusTo={wizardReturnFocus}
        />
      </div>
    )
  }

  const isDeleted = account.deleted_at !== null
  const isPaused = account.status === 'paused_user'
  const pausedByAdmin = account.status === 'paused_admin'
  const canPause = !isDeleted && (account.status === 'active' || isPaused)

  return (
    <div className="uapp-page uapp-page--form uapp-page--account">
      <Link className="uapp-hero-link uapp-back-link" to="/app">
        ‹ 返回主页
      </Link>
      <h1 className="uapp-greeting">签到账号</h1>

      <div className="uapp-account-grid">
      <section
        className={`uapp-hero uapp-hero--${userStatusTone(account.status, isDeleted)} uapp-account-hero`}
        aria-label="账号身份"
      >
        <div className="uapp-hero-identity">
          <div className="uapp-hero-photo uapp-hero-photo--empty">
            <Icon name="user" size={22} />
          </div>
          <div className="uapp-hero-identity-text">
            <h2 className="uapp-hero-name">
              {displayName(account.name, account.phone_masked)}
            </h2>
            <p className="uapp-hero-meta">
              {schoolName ? `${schoolName} · ` : ''}
              {account.phone_masked}
            </p>
            <p className="uapp-hero-stats">
              创建 <span className="uapp-date">{account.created_at.slice(0, 10)}</span> · 更新{' '}
              <span className="uapp-date">{account.updated_at.slice(0, 10)}</span>
              {account.reviewed_at ? (
                <>
                  {' '}
                  · 审核 <span className="uapp-date">{account.reviewed_at.slice(0, 10)}</span>
                </>
              ) : null}
            </p>
          </div>
          <span className={statusClass(account.status, isDeleted)}>
            <span className="uapp-status-dot" />
            {userStatusLabel(account.status, isDeleted)}
          </span>
        </div>
        {account.reject_reason ? (
          <p className="uapp-hero-note">驳回理由：{account.reject_reason}</p>
        ) : null}
      </section>

      <div className="uapp-account-main">
      {actionError ? (
        <div className="uapp-alert-block">
          <Alert tone="danger">{actionError}</Alert>
        </div>
      ) : null}

      <section className="uapp-card">
        <div className="uapp-card-head">
          <h2 className="uapp-card-title">账号资料</h2>
        </div>
        <div className="uapp-rows">
          <button
            type="button"
            className="uapp-row"
            disabled={isDeleted}
            onClick={(event) => {
              setEditReturnFocus(event.currentTarget)
              setActionError(null)
              setEditKind('remark')
            }}
          >
            <span className="uapp-row-icon"><Icon name="edit" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">备注名</span>
              <span className="uapp-row-sub">
                {account.name ? `当前「${account.name}」` : '用于主页与历史快速区分'}
              </span>
            </span>
            <span className="uapp-row-chevron">›</span>
          </button>
          <button
            type="button"
            className="uapp-row"
            disabled={isDeleted}
            onClick={(event) => {
              setEditReturnFocus(event.currentTarget)
              setActionError(null)
              setEditKind('credentials')
            }}
          >
            <span className="uapp-row-icon"><Icon name="key" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">易班资料</span>
              <span className="uapp-row-sub">
                {account.phone_masked} · 更新手机号或平台口令
              </span>
            </span>
            <span className="uapp-row-chevron">›</span>
          </button>
        </div>
      </section>

      <section className="uapp-card">
        <div className="uapp-card-head">
          <h2 className="uapp-card-title">设备</h2>
          <span className="uapp-row-value">只读</span>
        </div>
        <div className="uapp-rows">
          <div className="uapp-kv-row">
            <span className="uapp-row-icon"><Icon name="camera" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">机型</span>
              <span className="uapp-row-sub">{account.phone_model || '未知'}</span>
            </span>
          </div>
          <div className="uapp-kv-row">
            <span className="uapp-row-icon"><Icon name="shield" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">设备码</span>
              <span className="uapp-row-sub">
                {deviceMasked ?? '只能通过刷新设备读取'}
              </span>
            </span>
          </div>
          <button
            type="button"
            className="uapp-row"
            disabled={isDeleted}
            onClick={(event) => {
              setReauthReturnFocus(event.currentTarget)
              setReauthError(null)
              setReauthKind('refresh')
            }}
          >
            <span className="uapp-row-icon"><Icon name="refresh" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">刷新设备</span>
              <span className="uapp-row-sub">从易班重新读取，不修改账号</span>
            </span>
            <span className="uapp-row-chevron">›</span>
          </button>
        </div>
      </section>

      <section className="uapp-card">
        <div className="uapp-card-head">
          <h2 className="uapp-card-title">现场照片</h2>
          <span className="uapp-card-head-side">
            <span className="uapp-row-value">{photos.length} 张</span>
            <button
              type="button"
              className="uapp-card-action"
              disabled={photoBusy}
              onClick={() => uploadRef.current?.click()}
            >
              {photoBusy ? '上传中…' : '添加照片'}
            </button>
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
          <div>
            <p className="uapp-card-sub">照片读取失败。</p>
            <button
              type="button"
              className="uapp-card-action"
              onClick={() => void photosQuery.refetch()}
            >
              重试
            </button>
          </div>
        ) : photos.length === 0 ? (
          <p className="uapp-card-sub">还没有现场照片；仅用于签到审核，管理员可查看。</p>
        ) : (
          <div className="uapp-photo-grid">
            {photos.map((photo) => (
              <div className="uapp-photo-tile" key={photo.id}>
                <img src={photoBytesPath(photo.id)} alt="现场照片" />
                <button
                  type="button"
                  className="uapp-photo-delete"
                  aria-label="删除照片"
                  onClick={() => setPhotoToDelete(photo)}
                >
                  <span className="uapp-photo-delete-pill" aria-hidden="true">
                    ×
                  </span>
                </button>
              </div>
            ))}
          </div>
        )}
        {photoError ? (
          <p className="uapp-field-error" role="alert">
            {photoError}
          </p>
        ) : null}
        <p className="uapp-card-sub">仅用于签到审核；管理员可查看。保留期在设置里调整。</p>
      </section>
      </div>
      <div className="uapp-account-rail">
      {!isDeleted ? (
        <section className="uapp-card uapp-overview">
          <div className="uapp-card-head">
            <h2 className="uapp-card-title">签到概览</h2>
            <Link className="uapp-card-action" to="/app/history">
              历史 ›
            </Link>
          </div>
          <div className="uapp-overview-today">
            <span
              className={`uapp-day-cell uapp-day-cell--${todayMark}`}
              role="img"
              aria-label={todayText}
            >
              {todayMark === 'ok' ? <Icon name="check" size={15} /> : null}
              {todayMark === 'missed' ? <Icon name="close" size={14} /> : null}
              {todayMark === 'skipped' ? <Icon name="trendFlat" size={14} /> : null}
              {todayMark === 'pending' ? <i className="uapp-day-pending-dot" aria-hidden="true" /> : null}
            </span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">{todayText}</span>
              {todaySub ? <span className="uapp-row-sub">{todaySub}</span> : null}
            </span>
          </div>
          <WeekStrip
            days={calendarQuery.data?.days ?? []}
            today={calendarQuery.data?.today ?? today}
            hideLink
          />
          <div className="uapp-overview-next">
            <Icon name="clock" size={14} />
            <span>{nextRunText(prefQuery.data)}</span>
            {prefQuery.data?.window ? (
              <span className="uapp-row-sub">
                窗口 {prefQuery.data.window.start}–{prefQuery.data.window.end}
              </span>
            ) : null}
          </div>
        </section>
      ) : null}
      <section className="uapp-card">
        <h2 className="uapp-card-title">控制</h2>
        <div className="uapp-rows">
          <button
            type="button"
            className="uapp-row"
            disabled={!canPause || pausedByAdmin || pauseMutation.isPending}
            onClick={() => pauseMutation.mutate(!isPaused)}
          >
            <span className="uapp-row-icon"><Icon name="pause" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">
                {isPaused ? '恢复签到' : '暂停签到'}
              </span>
              <span className="uapp-row-sub">
                {pausedByAdmin
                  ? '管理员暂停，请联系管理员'
                  : isPaused
                    ? '恢复后从下一次开始'
                    : '暂停期间不签到'}
              </span>
            </span>
          </button>
          <button
            type="button"
            className="uapp-row"
            disabled={isDeleted || verifyMutation.isPending}
            onClick={(event) => {
              setVerifyReturnFocus(event.currentTarget)
              setActionError(null)
              verifyMutation.mutate()
            }}
          >
            <span className="uapp-row-icon"><Icon name="shield" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">验证易班凭据</span>
              <span className="uapp-row-sub">只读验证，不修改账号</span>
            </span>
          </button>
          <button
            type="button"
            className="uapp-row"
            disabled={isDeleted || personMutation.isPending}
            onClick={(event) => {
              setPersonReturnFocus(event.currentTarget)
              setActionError(null)
              personMutation.mutate()
            }}
          >
            <span className="uapp-row-icon"><Icon name="user" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">查看平台姓名</span>
              <span className="uapp-row-sub">按需读取，不保存</span>
            </span>
          </button>
          <div className="uapp-kv-row">
            <span className="uapp-row-icon"><Icon name="mapPin" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">签到地点</span>
              <span className="uapp-row-sub">
                {locationQuery.isError
                  ? '地点读取失败'
                  : locationQuery.data?.mode === 'fixed'
                    ? '固定位置（由管理员设置）'
                    : '自动定位'}
              </span>
            </span>
          </div>
          <button
            type="button"
            className="uapp-row uapp-row--danger"
            onClick={(event) => {
              setReauthReturnFocus(event.currentTarget)
              setReauthError(null)
              setReauthKind(isDeleted ? 'restore' : 'delete')
            }}
          >
            <span className="uapp-row-icon"><Icon name="trash" size={16} /></span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">
                {isDeleted ? '恢复账号' : '删除账号'}
              </span>
              <span className="uapp-row-sub">
                {isDeleted ? '7 天内可恢复' : '停止签到、隐藏凭据，7 天内可恢复'}
              </span>
            </span>
          </button>
        </div>
      </section>
      </div>
      </div>

      <EditModal
        open={editKind !== null}
        kind={editKind ?? 'remark'}
        account={account}
        pending={updateMutation.isPending}
        error={editKind === null ? null : actionError}
        returnFocusTo={editReturnFocus}
        onClose={() => {
          setEditKind(null)
          setActionError(null)
        }}
        onSubmit={(values) => {
          if (editKind === null) return
          setActionError(null)
          updateMutation.mutate({ values, kind: editKind })
        }}
      />

      <ReauthDialog
        open={reauthKind !== null}
        onOpenChange={(open) => {
          if (!open) closeReauth()
        }}
        title={
          reauthKind === 'delete'
            ? '删除账号'
            : reauthKind === 'restore'
              ? '恢复账号'
              : '刷新设备'
        }
        description={
          reauthKind === 'delete'
            ? '停止签到、隐藏凭据，7 天内可以恢复。需要输入本站登录口令。'
            : reauthKind === 'restore'
              ? '恢复后继续自动签到。需要输入本站登录口令。'
              : '从易班重新读取设备信息，不修改账号。需要输入本站登录口令。'
        }
        confirmLabel={
          reauthKind === 'delete' ? '删除账号' : reauthKind === 'restore' ? '恢复账号' : '刷新设备'
        }
        danger={reauthKind === 'delete'}
        pending={reauthMutation.isPending}
        error={reauthError}
        returnFocusTo={reauthReturnFocus}
        onConfirm={(password) => {
          if (reauthKind === null) return
          setReauthError(null)
          reauthMutation.mutate({ kind: reauthKind, password })
        }}
      />

      <Modal
        open={verifyOpen}
        onOpenChange={setVerifyOpen}
        title="验证结果"
        returnFocusTo={verifyReturnFocus}
        footer={
          <button
            type="button"
            className="uapp-button uapp-button--secondary"
            onClick={() => setVerifyOpen(false)}
          >
            知道了
          </button>
        }
      >
        <p className="uapp-hero-note">
          {verifyMutation.data ? verifyText(verifyMutation.data) : ''}
        </p>
      </Modal>

      <Modal
        open={personOpen}
        onOpenChange={setPersonOpen}
        title="平台姓名"
        returnFocusTo={personReturnFocus}
        description="只显示本次读取结果，不保存到账号。"
        footer={
          <button
            type="button"
            className="uapp-button uapp-button--secondary"
            onClick={() => setPersonOpen(false)}
          >
            关闭
          </button>
        }
      >
        <p className="uapp-hero-title" style={{ fontSize: 20 }}>
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
        footer={
          <>
            <button
              type="button"
              className="uapp-button uapp-button--ghost"
              onClick={() => setPhotoToDelete(null)}
            >
              取消
            </button>
            <button
              type="button"
              className="uapp-button uapp-button--danger"
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
        <p className="uapp-hero-note">确定删除这张现场照片吗？</p>
      </Modal>

      <AccountWizard
        key={wizardSession}
        open={wizardOpen}
        onOpenChange={closeWizard}
        returnFocusTo={wizardReturnFocus}
      />
    </div>
  )
}
