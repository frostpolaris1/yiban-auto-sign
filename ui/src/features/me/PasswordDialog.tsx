/**
 * **功能**
 * 修改登录密码弹窗：按会话角色的策略校验，成功后关合并回设置。
 *
 * **归属**
 * `features/me` 域容器：策略读取、就地校验与提交在此；设置行与深链共用。
 *
 * **口径**
 * 规则来自 `GET /api/auth/password-policy`，不硬编码。
 * 新口令要满足 min_chars / min_classes / max_bytes；确认只在前端比对。
 * 401 先经 me 探针定论：会话失效走登录页；口令错落当前口令字段。
 * 409 是新口令与当前相同；429 是连续失败限流。
 * 关合即清空三个字段：口令不留到下次打开。
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { Alert } from '@/components/ui/Alert'
import { PasswordInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { notifySuccess } from '@/components/ui/toast'
import {
  changePassword,
  getPasswordPolicy,
  SessionExpiredError,
  type SessionUser,
} from '@/lib/api/auth'
import { ApiError } from '@/lib/api/client'
import { expireSession, ME_QUERY_KEY } from '@/lib/session'

function classCount(value: string): number {
  return (
    Number(/[A-Z]/.test(value)) +
    Number(/[a-z]/.test(value)) +
    Number(/[0-9]/.test(value)) +
    Number(/[^A-Za-z0-9\s]/.test(value))
  )
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).length
}

export function PasswordDialog({
  open,
  onOpenChange,
  returnFocusTo,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  returnFocusTo?: HTMLElement | null
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const policyQuery = useQuery({
    queryKey: ['password-policy'],
    queryFn: getPasswordPolicy,
    staleTime: 60_000,
    retry: false,
  })

  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [fieldError, setFieldError] = useState<string | null>(null)
  // 提交过一次才标红未达标项：打开时不预判失败。
  const [attempted, setAttempted] = useState(false)

  // 口令不留存：关合即清字段与错误。
  useEffect(() => {
    if (open) return
    setCurrent('')
    setNext('')
    setConfirm('')
    setError(null)
    setFieldError(null)
    setAttempted(false)
  }, [open])

  const policy = policyQuery.data
  const classes = classCount(next)
  const lengthOk = policy ? next.length >= policy.min_chars : false
  const classesOk = policy ? classes >= policy.min_classes : false
  const bytesOk = policy ? byteLength(next) <= policy.max_bytes : false

  const mutation = useMutation({
    mutationFn: () =>
      changePassword({ current_password: current, new_password: next }),
    onSuccess: () => {
      setError(null)
      setFieldError(null)
      // 服务端已把 must_change_password 置 false；同步壳缓存让提示条立即消失。
      queryClient.setQueryData<SessionUser>(ME_QUERY_KEY, (previous) =>
        previous ? { ...previous, must_change_password: false } : previous,
      )
      notifySuccess('密码已修改')
      onOpenChange(false)
    },
    onError: (err) => {
      if (err instanceof SessionExpiredError) {
        expireSession((options) => void navigate(options))
        return
      }
      if (err instanceof ApiError) {
        if (err.status === 401) {
          setFieldError('当前密码不正确')
          return
        }
        if (err.status === 409) {
          setFieldError('新密码不能与当前密码相同')
          return
        }
        if (err.status === 429) {
          setError('尝试过多，请稍后再试。')
          return
        }
        if (err.status === 400) {
          setError('新密码不符合规则，请按提示修改。')
          return
        }
      }
      setError('修改失败，请稍后重试。')
    },
  })

  const submit = () => {
    setError(null)
    setFieldError(null)
    setAttempted(true)
    if (!policy) {
      setError('密码规则还没读到，请重试。')
      return
    }
    if (current === '') {
      setError('请输入当前密码。')
      document.getElementById('uapp-pw-current')?.focus()
      return
    }
    if (!lengthOk || !classesOk || !bytesOk) {
      setError('新密码不符合规则，请按提示修改。')
      document.getElementById('uapp-pw-next')?.focus()
      return
    }
    if (next !== confirm) {
      setError('两次输入的新密码不一致。')
      document.getElementById('uapp-pw-confirm')?.focus()
      return
    }
    if (next === current) {
      setError('新密码不能与当前密码相同。')
      document.getElementById('uapp-pw-next')?.focus()
      return
    }
    mutation.mutate()
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="修改密码"
      description="修改后，下次登录用新密码。"
      returnFocusTo={returnFocusTo}
      footer={
        <button
          type="button"
          className="uapp-button uapp-button--primary"
          disabled={mutation.isPending || !policy}
          onClick={submit}
        >
          {mutation.isPending ? '保存中…' : '保存新密码'}
        </button>
      }
    >
      <div className="uapp-set-dialog">
        {policyQuery.isError ? (
          <div className="uapp-alert-block">
            <Alert tone="danger">密码规则读取失败，暂时不能修改。</Alert>
            <button
              type="button"
              className="uapp-card-action"
              onClick={() => void policyQuery.refetch()}
            >
              重试
            </button>
          </div>
        ) : null}

        {error ? (
          <div className="uapp-alert-block">
            <Alert tone="danger">{error}</Alert>
          </div>
        ) : null}

        <div className="uapp-field">
          <PasswordInput
            id="uapp-pw-current"
            label="当前密码"
            value={current}
            autoComplete="current-password"
            error={fieldError ?? undefined}
            onChange={(event) => {
              setCurrent(event.target.value)
              setFieldError(null)
            }}
          />
        </div>
        <div className="uapp-field">
          <PasswordInput
            id="uapp-pw-next"
            label="新密码"
            value={next}
            autoComplete="new-password"
            onChange={(event) => setNext(event.target.value)}
          />
        </div>
        <div className="uapp-field">
          <PasswordInput
            id="uapp-pw-confirm"
            label="确认新密码"
            value={confirm}
            autoComplete="new-password"
            onChange={(event) => setConfirm(event.target.value)}
          />
        </div>

        {policy ? (
          <div className="uapp-pw-meter" aria-live="polite">
            <span className={lengthOk ? 'is-ok' : attempted ? 'is-bad' : undefined}>
              至少 {policy.min_chars} 位
            </span>
            <span className={classesOk ? 'is-ok' : attempted ? 'is-bad' : undefined}>
              至少 {policy.min_classes} 类字符（{classes}/{policy.min_classes}）
            </span>
            <span className={bytesOk ? 'is-ok' : attempted ? 'is-bad' : undefined}>
              不超过 {policy.max_bytes} 字节
            </span>
          </div>
        ) : null}
      </div>
    </Modal>
  )
}