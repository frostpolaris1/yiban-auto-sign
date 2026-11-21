/**
 * **功能**
 * 登录表单：邮箱、密码、提交按钮。接 `POST /api/auth/login`。
 *
 * **归属**
 * `features/auth` 域组件。
 *
 * **口径**
 * 提交体只含契约 `LoginRequest` 的两个字段。
 * 成功：写入会话缓存 → 导航到 `redirect` 目标或 `/`。
 * 软删账号登录是统一 401（后端防枚举）：401 后用同邮箱+口令调一次
 * `POST /api/me/restore`，成功则重发登录——「登录即自动恢复」由前端编排。
 * restore 失败照旧上抛：文案仍走 `loginErrorMessage`，不泄漏「被删除」状态。
 * 失败：就地显示错误并聚焦错误块，不导航；改任一字段即收起旧错误。
 * **不得添加「保持登录」选项**。会话期限由服务端定。
 * 密码框提供显隐开关；大写锁定时给出可见提示。
 */

import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Icon } from '@/components/ui/Icon'
import { PasswordInput, TextInput } from '@/components/ui/Input'
import { login, restoreMyAccount } from '@/lib/api/auth'
import { ApiError } from '@/lib/api/client'
import { resolveLanding } from '@/lib/landing'
import { clearSessionExpired, ME_QUERY_KEY, SHOWCASE } from '@/lib/session'
import { buildLoginBody, loginErrorMessage } from './login'

type LoginFormProps = {
  /** 守卫写入的回跳地址。已净化，仍再净化一次。 */
  redirect?: string
  /** 会话过期提示。登录成功后清除。 */
  expired?: boolean
  /** 提交开始。登录页据此暂停「已登录即跳转首页」的兜底守卫。 */
  onSignInStart?: () => void
  /** 提交失败。登录页据此恢复兜底守卫，允许重试。 */
  onSignInFailed?: () => void
}

export function LoginForm({ redirect, expired, onSignInStart, onSignInFailed }: LoginFormProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [capsLock, setCapsLock] = useState(false)
  const errorRef = useRef<HTMLDivElement>(null)

  const mutation = useMutation({
    mutationFn: async () => {
      // 展示模式：无后端，任意输入都直接进入（合成会话由 useMe 常驻提供）。
      if (SHOWCASE) {
        await new Promise((resolve) => setTimeout(resolve, 400))
        return null
      }
      const body = buildLoginBody({ email, password })
      try {
        return await login(body)
      } catch (error) {
        // 软删宽限内的账号在登录接口是统一 401；自动尝试恢复一次再重发登录。
        // restore 自带 7 天窗口与同口令校验；任何失败继续上抛，文案不区分原因。
        if (error instanceof ApiError && error.status === 401) {
          await restoreMyAccount({ email: body.email, password: body.password })
          return await login(body)
        }
        throw error
      }
    },
    onSuccess: (me) => {
      clearSessionExpired()
      // 正常模式：登录响应即会话，先写缓存再导航——壳的守卫读同一份
      // `['me']`，不会把刚登录的人弹回登录页（与切片 3 行为一致）。
      // 展示模式：会话由 useMe 常驻提供，这里只导航。
      if (me) {
        queryClient.setQueryData(ME_QUERY_KEY, me)
        void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY })
      }
      // 角色决定落地：user → /app；master/admin → /。
      // 回跳地址只在该角色可去的面内生效；登录/注册页自身不作为目标。
      const target = me ? resolveLanding(me.role, redirect) : '/'
      void navigate({ href: target, replace: true })
    },
    onError: () => {
      // 失败可重试：恢复登录页的兜底守卫。
      onSignInFailed?.()
    },
  })

  const pending = mutation.isPending

  // 错误块只在失败次数变化时接焦点：同一次失败不重复抢焦点，
  // 重试再失败会重新落位。role=alert 负责播报，焦点负责定位。
  useEffect(() => {
    if (mutation.failureCount > 0) errorRef.current?.focus()
  }, [mutation.failureCount])

  /** 改动输入即收起上一次的失败提示，避免旧结论压着新输入。 */
  function clearRejectedError(): void {
    if (mutation.isError) mutation.reset()
  }

  function trackCapsLock(event: KeyboardEvent<HTMLInputElement>): void {
    setCapsLock(event.getModifierState('CapsLock'))
  }

  return (
    <>
      {expired ? (
        <div className="app-auth-notice">
          <Alert tone="warning">会话已过期，请重新登录</Alert>
        </div>
      ) : null}

      <h2>欢迎回来</h2>
      <p className="sub">登录后查看账号与今天的签到结果。</p>

      <form
        className="auth-form"
        onSubmit={(event) => {
          event.preventDefault()
          if (pending) return
          mutation.reset()
          onSignInStart?.()
          mutation.mutate()
        }}
      >
        <TextInput
          id="login-email"
          label="邮箱"
          type="email"
          name="email"
          inputMode="email"
          icon="mail"
          placeholder="name@example.com"
          autoComplete="username"
          required
          value={email}
          onChange={(event) => {
            setEmail(event.target.value)
            clearRejectedError()
          }}
        />

        <PasswordInput
          id="login-password"
          label="密码"
          name="password"
          placeholder="输入密码"
          autoComplete="current-password"
          enterKeyHint="go"
          required
          value={password}
          labelAction={
            <Link className="app-inline-link" to="/forgot-password">
              忘记密码？
            </Link>
          }
          onChange={(event) => {
            setPassword(event.target.value)
            clearRejectedError()
          }}
          onKeyDown={trackCapsLock}
          onKeyUp={trackCapsLock}
          onBlur={() => setCapsLock(false)}
        />

        {capsLock ? (
          <p className="app-caps-hint" role="status">
            <Icon name="alertCircle" size={12} />
            大写锁定已开启
          </p>
        ) : null}

        {mutation.isError ? (
          <div className="app-alert-block" ref={errorRef} tabIndex={-1}>
            <Alert tone="danger">{loginErrorMessage(mutation.error)}</Alert>
          </div>
        ) : null}

        <button
          className="btn btn--primary auth-submit"
          type="submit"
          disabled={pending}
          aria-busy={pending}
        >
          {pending ? '登录中…' : '登录'}
        </button>
      </form>

      <p className="auth-switch-hint">
        还没有账号？
        <Link className="app-inline-link" to="/register">
          创建一个
        </Link>
        ，或联系管理员开通。
      </p>

    </>
  )
}
