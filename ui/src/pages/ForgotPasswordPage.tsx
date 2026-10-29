/**
 * **功能**
 * 忘记密码页：输入注册邮箱，请求一次性重置链接。
 *
 * **归属**
 * `pages` 路由页：装配公开页外壳与表单，域逻辑在 `features/auth`。
 *
 * **口径**
 * 成功文案固定，不透露账号是否存在。
 * 400 归到邮箱字段；其余失败走信封文本或固定网络文案。
 * 提交后焦点落到结果提示；有错误时落到第一个出错的落点。
 */

import { useEffect, useRef, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { TextInput } from '@/components/ui/Input'
import { PublicAuthPage } from '@/features/auth/PublicAuthPage'
import { buildForgotBody, passwordErrorMessage } from '@/features/auth/password-reset'
import { forgotPassword } from '@/lib/api/auth'
import { ApiError } from '@/lib/api/client'

const EMAIL_FIELD_ID = 'forgot-email'

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [emailError, setEmailError] = useState<string | null>(null)
  const [generalError, setGeneralError] = useState<string | null>(null)
  const resultRef = useRef<HTMLHeadingElement>(null)
  const alertRef = useRef<HTMLDivElement>(null)

  const mutation = useMutation({
    mutationFn: () => forgotPassword(buildForgotBody({ email })),
    onError: (error) => {
      // 400 是邮箱本身不合格：归到字段。其余（413、5xx、断网）走通栏提示。
      if (error instanceof ApiError && error.status === 400) {
        setEmailError(passwordErrorMessage(error))
        setGeneralError(null)
        return
      }
      setEmailError(null)
      setGeneralError(passwordErrorMessage(error))
    },
  })

  const sent = mutation.isSuccess
  const pending = mutation.isPending

  // 成功：焦点给结果提示，读屏器随即播报固定文案。
  useEffect(() => {
    if (sent) resultRef.current?.focus()
  }, [sent])

  // 失败：焦点给第一个出错的落点。按 failureCount 复位，重试同样报错也会重新聚焦。
  useEffect(() => {
    if (mutation.failureCount === 0) return
    if (emailError) {
      document.getElementById(EMAIL_FIELD_ID)?.focus()
      return
    }
    if (generalError) alertRef.current?.focus()
  }, [mutation.failureCount, emailError, generalError])

  return (
    <PublicAuthPage>
      {sent ? (
        <>
          <h2 ref={resultRef} tabIndex={-1}>
            请检查邮箱
          </h2>
          <p className="sub">如果该邮箱已注册，我们已发送重置链接。</p>
          <div className="auth-form">
            <Link className="btn btn--primary auth-submit" to="/login">
              返回登录
            </Link>
          </div>
        </>
      ) : (
        <>
          <h2>忘记密码</h2>
          <p className="sub">输入注册邮箱，我们会发送一次性重置链接。</p>
          <form
            className="auth-form"
            onSubmit={(event) => {
              event.preventDefault()
              if (pending) return
              setEmailError(null)
              setGeneralError(null)
              mutation.mutate()
            }}
          >
            <TextInput
              id={EMAIL_FIELD_ID}
              label="邮箱"
              type="email"
              icon="mail"
              placeholder="name@example.com"
              autoComplete="email"
              required
              value={email}
              error={emailError ?? undefined}
              onChange={(event) => setEmail(event.target.value)}
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
              {pending ? '发送中…' : '发送重置链接'}
              <Icon name="arrowRight" />
            </Button>
          </form>

          <div className="auth-main-bottom">
            <Link to="/login">返回登录</Link>
          </div>
        </>
      )}
    </PublicAuthPage>
  )
}
