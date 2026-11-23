/**
 * **功能**
 * 用户端设置：账号、签到时间、通知、隐私、安全、账号数据与退出。
 *
 * **归属**
 * `features/me` 域容器：设置分组、保存与危险操作在此。
 *
 * **口径**
 * 只做个人设置；系统设置与管理面不出现。
 * 签到时间、失败邮件、照片保留期各自保存；失败留在行内。
 * 签到时间与改密以弹窗承载（少整页、多模态）；深链路由开同一弹窗。
 * 删除登录账号要二次口令；成功后清缓存回登录页，7 天内可用邮箱+口令恢复。
 * 已删除的签到账号在账号页恢复，本页只给入口。
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useRouterState } from '@tanstack/react-router'
import { useState } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Modal'
import { notifySuccess } from '@/components/ui/toast'
import { deleteMyAccount, logout } from '@/lib/api/auth'
import { ApiError } from '@/lib/api/client'
import { setMailNotify, setPhotoRetention } from '@/lib/api/me'
import type { PhotoRetention } from '@/lib/api/me'
import { clearSessionExpired, useMe } from '@/lib/session'
import { ME_KEYS, useMailNotify, useMyAccounts, usePhotoRetention, useTimePref } from './queries'
import { PasswordDialog } from './PasswordDialog'
import { ReauthDialog } from './ReauthDialog'
import { TimePrefDialog } from './TimePrefDialog'

const RETENTION_OPTIONS: Array<PhotoRetention['days']> = [30, 90, 180, 365]

function offsetClock(pref: { window: { start: string }; offset_min: number | null } | undefined): string {
  if (!pref) return '读取中…'
  if (pref.offset_min === null) return '自动分配'
  const [hour, minute] = pref.window.start.split(':').map(Number)
  const total = hour * 60 + minute + pref.offset_min
  const h = String(Math.floor(total / 60)).padStart(2, '0')
  const m = String(total % 60).padStart(2, '0')
  return `${h}:${m} 左右`
}

function actionErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '请输入本站登录口令。'
    if (error.status === 403) return '口令不正确。'
    if (error.status === 429) return '尝试过多，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    return '操作失败，请稍后重试。'
  }
  return '网络异常，请稍后重试。'
}

export function SettingsView({
  initialDialog,
}: {
  initialDialog?: 'time-pref' | 'password'
} = {}) {
  const navigate = useNavigate()
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const queryClient = useQueryClient()
  const timePrefQuery = useTimePref()
  const mailQuery = useMailNotify()
  const retentionQuery = usePhotoRetention()
  const accountsQuery = useMyAccounts()
  const meQuery = useMe()

  const [retentionOpen, setRetentionOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [retentionError, setRetentionError] = useState<string | null>(null)
  const [retentionReturnFocus, setRetentionReturnFocus] = useState<HTMLElement | null>(null)
  const [deleteReturnFocus, setDeleteReturnFocus] = useState<HTMLElement | null>(null)
  const [inlineError, setInlineError] = useState<string | null>(null)

  // 子表单弹窗化：设置行与深链共用同一实例口径。
  const [timeOpen, setTimeOpen] = useState(initialDialog === 'time-pref')
  const [passwordOpen, setPasswordOpen] = useState(initialDialog === 'password')
  const [timeReturnFocus, setTimeReturnFocus] = useState<HTMLElement | null>(null)
  const [passwordReturnFocus, setPasswordReturnFocus] = useState<HTMLElement | null>(null)
  const closeTime = (open: boolean) => {
    if (open) return
    setTimeOpen(false)
    // 深链关闭后归位到设置页，URL 与内容一致。
    if (pathname === '/app/settings/time-pref') {
      void navigate({ to: '/app/settings', replace: true })
    }
  }
  const closePassword = (open: boolean) => {
    if (open) return
    setPasswordOpen(false)
    if (pathname === '/app/settings/password') {
      void navigate({ to: '/app/settings', replace: true })
    }
  }

  const deletedAccount =
    accountsQuery.data?.items.find((item) => item.deleted_at !== null) ?? null

  const mailMutation = useMutation({
    mutationFn: setMailNotify,
    onSuccess: (data) => {
      queryClient.setQueryData(ME_KEYS.mailNotify, data)
      setInlineError(null)
      notifySuccess(data.enabled ? '已开启失败邮件' : '已关闭失败邮件')
    },
    onError: (error) => setInlineError(actionErrorText(error)),
  })

  const retentionMutation = useMutation({
    mutationFn: setPhotoRetention,
    onSuccess: (data) => {
      queryClient.setQueryData(ME_KEYS.photoRetention, data)
      setRetentionOpen(false)
      setRetentionError(null)
      setInlineError(null)
      notifySuccess(`照片保留期已设为 ${data.days} 天`)
    },
    onError: (error) => setRetentionError(actionErrorText(error)),
  })

  const deleteMutation = useMutation({
    mutationFn: (password: string) => deleteMyAccount({ current_password: password }),
    onSuccess: () => {
      clearSessionExpired()
      queryClient.clear()
      void navigate({ to: '/login', replace: true })
    },
    onError: (error) => setDeleteError(actionErrorText(error)),
  })

  const logoutMutation = useMutation({
    mutationFn: logout,
    onSuccess: () => {
      clearSessionExpired()
      queryClient.clear()
      void navigate({ to: '/login', replace: true })
    },
    onError: (error) => setInlineError(actionErrorText(error)),
  })

  const pref = timePrefQuery.data
  const mailEnabled = mailQuery.data?.enabled ?? true
  const retention = retentionQuery.data?.days ?? 90

  return (
    <div className="uapp-page uapp-page--settings">
      <div className="uapp-settings-head">
        <div className="uapp-home-eyebrow">个人偏好与安全</div>
        <h1 className="uapp-greeting">设置</h1>
      </div>

      {inlineError ? (
        <div className="uapp-alert-block">
          <Alert tone="danger">{inlineError}</Alert>
        </div>
      ) : null}

      <div className="uapp-settings-grid">
      <section className="uapp-card uapp-set-pref">
        <h2 className="uapp-card-title">偏好</h2>
        <div className="uapp-rows">
          <button
            type="button"
            className="uapp-row"
            onClick={(event) => {
              setTimeReturnFocus(event.currentTarget)
              setTimeOpen(true)
            }}
          >
            <span className="uapp-row-icon">
              <Icon name="clock" size={16} />
            </span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">签到时间</span>
              <span className="uapp-row-sub">
                {timePrefQuery.isError
                  ? '读取失败'
                  : pref?.window
                    ? `有效窗口 ${pref.window.start}–${pref.window.end}`
                    : '读取中…'}
              </span>
            </span>
            <span className="uapp-row-value">
              {timePrefQuery.isError ? '—' : offsetClock(pref)}
            </span>
            <span className="uapp-row-chevron">›</span>
          </button>

          <div className="uapp-row">
            <span className="uapp-row-icon">
              <Icon name="bell" size={16} />
            </span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">签到失败邮件</span>
              <span className="uapp-row-sub">只发给你，不含易班口令</span>
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={mailEnabled}
              aria-label="签到失败邮件"
              className={mailEnabled ? 'uapp-switch is-on' : 'uapp-switch'}
              disabled={mailQuery.isPending || mailMutation.isPending}
              onClick={() => {
                setInlineError(null)
                mailMutation.mutate(!mailEnabled)
              }}
            >
              <span className="uapp-switch-knob" />
            </button>
          </div>

          <button
            type="button"
            className="uapp-row"
            disabled={retentionQuery.isPending}
            onClick={(event) => {
              setRetentionReturnFocus(event.currentTarget)
              setInlineError(null)
              setRetentionError(null)
              setRetentionOpen(true)
            }}
          >
            <span className="uapp-row-icon">
              <Icon name="shield" size={16} />
            </span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">照片保留期</span>
              <span className="uapp-row-sub">过期照片自动删除</span>
            </span>
            <span className="uapp-row-value">{retention} 天</span>
            <span className="uapp-row-chevron">›</span>
          </button>

        </div>
        <p className="uapp-card-sub uapp-set-note">现场照片仅你和管理员可见，用于签到审核。</p>
      </section>

      <section className="uapp-card uapp-set-account">
        <h2 className="uapp-card-title">账号与安全</h2>
        <div className="uapp-rows">
          <div className="uapp-kv-row">
            <span className="uapp-row-icon">
              <Icon name="user" size={16} />
            </span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">登录账号</span>
              <span className="uapp-row-sub">
                {meQuery.data?.email ?? '邮箱与安全设置'}
              </span>
            </span>
            <button
              type="button"
              className="uapp-card-action"
              onClick={(event) => {
                setPasswordReturnFocus(event.currentTarget)
                setPasswordOpen(true)
              }}
            >
              修改密码
            </button>
          </div>

          <Link className="uapp-row" to="/app/account">
            <span className="uapp-row-icon">
              <Icon name="calendar" size={16} />
            </span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">账号管理</span>
              <span className="uapp-row-sub">签到账号 · 暂停 · 设备 · 照片</span>
            </span>
            <span className="uapp-row-chevron">›</span>
          </Link>

          {deletedAccount ? (
            <Link className="uapp-row" to="/app/account">
              <span className="uapp-row-icon">
                <Icon name="rotateCcw" size={16} />
              </span>
              <span className="uapp-row-main">
                <span className="uapp-row-label">已删除的签到账号</span>
                <span className="uapp-row-sub">7 天内可在账号页恢复</span>
              </span>
              <span className="uapp-row-chevron">›</span>
            </Link>
          ) : null}

          <button
            type="button"
            className="uapp-row"
            disabled={logoutMutation.isPending}
            onClick={() => {
              setInlineError(null)
              logoutMutation.mutate()
            }}
          >
            <span className="uapp-row-icon">
              <Icon name="logout" size={16} />
            </span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">
                {logoutMutation.isPending ? '退出中…' : '退出登录'}
              </span>
            </span>
          </button>

          <button
            type="button"
            className="uapp-row uapp-row--danger"
            onClick={(event) => {
              setDeleteReturnFocus(event.currentTarget)
              setDeleteError(null)
              setDeleteOpen(true)
            }}
          >
            <span className="uapp-row-icon">
              <Icon name="trash" size={16} />
            </span>
            <span className="uapp-row-main">
              <span className="uapp-row-label">删除登录账号</span>
              <span className="uapp-row-sub">7 天内可用邮箱与口令恢复</span>
            </span>
          </button>
        </div>
      </section>

      </div>

      <Modal
        open={retentionOpen}
        onOpenChange={setRetentionOpen}
        title="照片保留期"
        returnFocusTo={retentionReturnFocus}
        description="过期后照片会自动删除；只影响现场照片。"
      >
        {retentionError ? (
          <div className="uapp-alert-block">
            <Alert tone="danger">{retentionError}</Alert>
          </div>
        ) : null}
        <div className="uapp-retention-options">
          {RETENTION_OPTIONS.map((days) => (
            <button
              key={days}
              type="button"
              aria-pressed={days === retention}
              className={
                days === retention
                  ? 'uapp-retention-option active'
                  : 'uapp-retention-option'
              }
              disabled={retentionMutation.isPending}
              onClick={() => retentionMutation.mutate(days)}
            >
              {days} 天
            </button>
          ))}
        </div>
      </Modal>

      <ReauthDialog
        open={deleteOpen}
        onOpenChange={(open) => {
          if (!open) setDeleteOpen(false)
        }}
        title="删除登录账号"
        description="会注销登录并删除签到账号；7 天内可用邮箱与口令恢复。需要输入本站登录口令。"
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

      <TimePrefDialog
        open={timeOpen}
        onOpenChange={closeTime}
        returnFocusTo={timeReturnFocus}
      />

      <PasswordDialog
        open={passwordOpen}
        onOpenChange={closePassword}
        returnFocusTo={passwordReturnFocus}
      />
    </div>
  )
}
