/**
 * **功能**
 * 修改密码卡：当前口令、新口令、确认新口令。
 *
 * **归属**
 * `features/settings` 展示层：表单与提交；校验与文案在 `change-password.ts`。
 *
 * **口径**
 * 就地校验先跑：有本地错误即不发请求。
 * 401 先经 me 探针定论：会话失效 ⇒ 统一过期处置；会话仍在 ⇒ 落当前密码字段。
 * 409 落新密码字段，其余错误进通栏提示。
 * 成功后清空三个字段、刷新会话缓存（提示条随之消失）、发一条 toast。
 * 展示构建下提交按钮置灰；onSubmit 另有只读守卫，回车或脚本提交都发不出请求。
 * 表单带 `noValidate`：错误文案只由本组件给出，不混浏览器气泡。
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { TextInput } from '@/components/ui/Input'
import { notifySuccess } from '@/components/ui/toast'
import { SessionExpiredError, changePassword, type SessionUser } from '@/lib/api/auth'
import { ME_QUERY_KEY, expireSession } from '@/lib/session'
import { READ_ONLY_REASON, SHOWCASE } from '@/lib/showcase-mode'
import {
  PASSWORD_CHANGED_TOAST,
  buildChangePasswordBody,
  changePasswordErrorMessage,
  changePasswordFieldError,
  validateChangePassword,
  type ChangePasswordErrors,
  type ChangePasswordField,
} from './change-password'

const CURRENT_ID = 'settings-current-password'
const NEXT_ID = 'settings-new-password'
const CONFIRM_ID = 'settings-confirm-password'

const FIELD_ORDER: readonly ChangePasswordField[] = ['current', 'next', 'confirm']

const FIELD_IDS: Record<ChangePasswordField, string> = {
  current: CURRENT_ID,
  next: NEXT_ID,
  confirm: CONFIRM_ID,
}

type ChangePasswordCardProps = {
  className?: string
}

export function ChangePasswordCard({ className }: ChangePasswordCardProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [errors, setErrors] = useState<ChangePasswordErrors>({})
  const [generalError, setGeneralError] = useState<string | null>(null)
  const [submitCount, setSubmitCount] = useState(0)
  const alertRef = useRef<HTMLDivElement>(null)

  const readOnlyReason = SHOWCASE ? READ_ONLY_REASON : null

  const mutation = useMutation({
    mutationFn: () => changePassword(buildChangePasswordBody({ current, next, confirm })),
    onSuccess: () => {
      setCurrent('')
      setNext('')
      setConfirm('')
      setErrors({})
      setGeneralError(null)
      // 服务端已把 must_change_password 置 false；壳读同一份缓存，提示条随之消失。
      queryClient.setQueryData<SessionUser>(ME_QUERY_KEY, (previous) =>
        previous ? { ...previous, must_change_password: false } : previous,
      )
      notifySuccess(PASSWORD_CHANGED_TOAST)
      document.getElementById(CURRENT_ID)?.focus()
    },
    onError: (error) => {
      // 会话失效不是口令错：统一过期处置，绝不给「当前密码不正确」。
      if (error instanceof SessionExpiredError) {
        setErrors({})
        setGeneralError(null)
        expireSession((options) => navigate(options))
        return
      }
      const fieldError = changePasswordFieldError(error)
      if (fieldError) {
        setErrors({ [fieldError.field]: fieldError.message })
        setGeneralError(null)
        return
      }
      setGeneralError(changePasswordErrorMessage(error))
    },
  })

  const pending = mutation.isPending

  // 失败后焦点给第一个出错的落点：先字段，后通栏。
  useEffect(() => {
    if (submitCount === 0) return
    const firstError = FIELD_ORDER.find((field) => errors[field] !== undefined)
    if (firstError) {
      document.getElementById(FIELD_IDS[firstError])?.focus()
      return
    }
    if (generalError) alertRef.current?.focus()
  }, [submitCount, mutation.failureCount, errors, generalError])

  return (
    <Card
      className={className}
      eyebrow="Security"
      title="修改密码"
      action={<span className="app-caption">仅本人可改</span>}
    >
      <p className="app-muted">
        修改后其他设备的登录会话将失效，当前会话保持登录。
      </p>

      <form
        noValidate
        className="app-form-stack"
        onSubmit={(event) => {
          event.preventDefault()
          // 只读守卫先于一切：禁用按钮挡不住回车与脚本提交。
          if (readOnlyReason) return
          if (pending) return
          mutation.reset()
          setSubmitCount((count) => count + 1)
          const nextErrors = validateChangePassword({ current, next, confirm })
          setErrors(nextErrors)
          setGeneralError(null)
          if (Object.keys(nextErrors).length > 0) return
          mutation.mutate()
        }}
      >
        <TextInput
          id={CURRENT_ID}
          label="当前密码"
          type="password"
          icon="shield"
          placeholder="••••••••"
          autoComplete="current-password"
          required
          value={current}
          error={errors.current}
          onChange={(event) => setCurrent(event.target.value)}
        />

        <TextInput
          id={NEXT_ID}
          label="新密码"
          type="password"
          icon="shield"
          placeholder="••••••••"
          autoComplete="new-password"
          required
          value={next}
          error={errors.next}
          onChange={(event) => setNext(event.target.value)}
        />

        <TextInput
          id={CONFIRM_ID}
          label="确认新密码"
          type="password"
          icon="shield"
          placeholder="••••••••"
          autoComplete="new-password"
          required
          value={confirm}
          error={errors.confirm}
          onChange={(event) => setConfirm(event.target.value)}
        />

        {generalError ? (
          <div className="app-alert-block" ref={alertRef} tabIndex={-1}>
            <Alert tone="danger">{generalError}</Alert>
          </div>
        ) : null}

        <div className="app-page-actions">
          <Button
            variant="primary"
            type="submit"
            disabled={pending || readOnlyReason !== null}
            title={readOnlyReason ?? undefined}
            aria-busy={pending}
          >
            {pending ? '提交中…' : '修改密码'}
          </Button>
          {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
        </div>
      </form>
    </Card>
  )
}
