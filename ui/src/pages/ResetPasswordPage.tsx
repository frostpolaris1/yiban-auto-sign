/**
 * **功能**
 * 重置密码页：从 `?token=` 读一次性令牌，设置新密码。
 *
 * **归属**
 * `pages` 路由页：装配公开页外壳与表单，域逻辑在 `features/auth`。
 *
 * **口径**
 * 令牌缺失或形状不合法：只显示链接失效，不发请求。
 * 两次输入不一致：就地提示，不发请求。
 * 401：显示链接已过期或已使用。该 401 不标记会话过期（分类在 `lib/api/auth.ts`）。
 * 提交后焦点落到结果提示；有错误时落到第一个出错的落点。
 */

import { useEffect, useRef, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/ui/Input'
import { PublicAuthPage } from '@/features/auth/PublicAuthPage'
import { buildResetBody, isValidResetToken, passwordErrorMessage } from '@/features/auth/password-reset'
import { resetPassword } from '@/lib/api/auth'
import { ApiError } from '@/lib/api/client'

const PASSWORD_FIELD_ID = 'reset-password'
const CONFIRM_FIELD_ID = 'reset-confirm'
const MISMATCH_MESSAGE = '两次输入的密码不一致'

type ResetPasswordPageProps = {
  /** 路由从查询串原样带来的令牌。此处判形状。 */
  token?: string
}

export function ResetPasswordPage({ token }: ResetPasswordPageProps) {
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [generalError, setGeneralError] = useState<string | null>(null)
  const [submitCount, setSubmitCount] = useState(0)
  const resultRef = useRef<HTMLHeadingElement>(null)
  const alertRef = useRef<HTMLDivElement>(null)

  const mutation = useMutation({
    mutationFn: () =>
      resetPassword(buildResetBody({ token: token ?? '', new_password: password })),
    onError: (error) => {
      // 401 是令牌问题，不是表单问题：页面切到「链接已过期或已使用」。
      if (error instanceof ApiError && error.status === 401) {
        setGeneralError(null)
        return
      }
      setGeneralError(passwordErrorMessage(error))
    },
  })

  const linkInvalid = !isValidResetToken(token)
  const done = mutation.isSuccess
  const expired = mutation.error instanceof ApiError && mutation.error.status === 401
  const pending = mutation.isPending

  // 终态：焦点给结果提示。
  useEffect(() => {
    if (done || expired) resultRef.current?.focus()
  }, [done, expired])

  // 失败：焦点给第一个出错的落点。submitCount 与 failureCount 都进依赖，
  // 同样的错误重复出现时也会重新聚焦。
  useEffect(() => {
    if (submitCount === 0) return
    if (confirmError) {
      document.getElementById(CONFIRM_FIELD_ID)?.focus()
      return
    }
    if (generalError) alertRef.current?.focus()
  }, [submitCount, mutation.failureCount, confirmError, generalError])

  if (linkInvalid) {
    return (
      <PublicAuthPage>
        <h2>链接不完整或已失效</h2>
        <p className="sub">请重新申请一次性重置链接。</p>
        <div className="auth-form">
          <Link className="btn btn--primary auth-submit" to="/forgot-password">
            重新申请
          </Link>
        </div>
        <div className="auth-main-bottom">
          <Link to="/login">返回登录</Link>
        </div>
      </PublicAuthPage>
    )
  }

  if (done) {
    return (
      <PublicAuthPage>
        <h2 ref={resultRef} tabIndex={-1}>
          密码已重置
        </h2>
        <p className="sub">请使用新密码登录控制台。</p>
        <div className="auth-form">
          <Link className="btn btn--primary auth-submit" to="/login">
            去登录
          </Link>
        </div>
      </PublicAuthPage>
    )
  }

  if (expired) {
    return (
      <PublicAuthPage>
        <h2 ref={resultRef} tabIndex={-1}>
          链接已过期或已使用
        </h2>
        <p className="sub">请重新申请一次性重置链接。</p>
        <div className="auth-form">
          <Link className="btn btn--primary auth-submit" to="/forgot-password">
            重新申请
          </Link>
        </div>
        <div className="auth-main-bottom">
          <Link to="/login">返回登录</Link>
        </div>
      </PublicAuthPage>
    )
  }

  return (
    <PublicAuthPage>
      <h2>重置密码</h2>
      <p className="sub">设置新的登录密码。重置后，该账号的全部登录会话将失效。</p>
      <form
        className="auth-form"
        onSubmit={(event) => {
          event.preventDefault()
          if (pending) return
          setSubmitCount((count) => count + 1)
          setGeneralError(null)
          if (password !== confirmation) {
            setConfirmError(MISMATCH_MESSAGE)
            return
          }
          setConfirmError(null)
          mutation.mutate()
        }}
      >
        <TextInput
          id={PASSWORD_FIELD_ID}
          label="新密码"
          type="password"
          icon="shield"
          placeholder="••••••••"
          autoComplete="new-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />

        <TextInput
          id={CONFIRM_FIELD_ID}
          label="确认密码"
          type="password"
          icon="shield"
          placeholder="••••••••"
          autoComplete="new-password"
          required
          value={confirmation}
          error={confirmError ?? undefined}
          onChange={(event) => setConfirmation(event.target.value)}
        />

        {generalError ? (
          <div className="app-alert-block" ref={alertRef} tabIndex={-1}>
            <Alert tone="danger">{generalError}</Alert>
          </div>
        ) : null}

        <Button
          variant="primary"
          type="submit"
          className="auth-submit"
          disabled={pending}
          aria-busy={pending}
        >
          {pending ? '重置中…' : '重置密码'}
        </Button>
      </form>

      <div className="auth-main-bottom">
        <Link to="/login">返回登录</Link>
      </div>
    </PublicAuthPage>
  )
}
