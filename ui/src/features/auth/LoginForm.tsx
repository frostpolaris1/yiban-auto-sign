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
 * 失败：就地显示错误，不导航。
 * **不得添加「保持登录」选项**。会话期限由服务端定。
 */

import { useState } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Icon } from '@/components/ui/Icon'
import { TextInput } from '@/components/ui/Input'
import { login } from '@/lib/api/auth'
import { sanitizeRedirect } from '@/lib/redirect'
import { clearSessionExpired, ME_QUERY_KEY, SHOWCASE } from '@/lib/session'
import { buildLoginBody, isLoginPath, loginErrorMessage } from './login'

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

  const mutation = useMutation({
    mutationFn: async () => {
      // 展示模式：无后端，任意输入都直接进入（合成会话由 useMe 常驻提供）。
      if (SHOWCASE) {
        await new Promise((resolve) => setTimeout(resolve, 400))
        return null
      }
      return login(buildLoginBody({ email, password }))
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
      // `/login` 的各种拼写（含 URI 编码）不能当回跳目标：命中落 `/`。
      // 成功路径必然离开本页，提交锁随卸载失效，无需复位。
      const target = sanitizeRedirect(redirect)
      void navigate({
        href: target && !isLoginPath(target) ? target : '/',
        replace: true,
      })
    },
    onError: () => {
      // 失败可重试：恢复登录页的兜底守卫。
      onSignInFailed?.()
    },
  })

  const pending = mutation.isPending

  return (
    <div className="auth-card">
      {expired ? (
        <div className="app-auth-notice">
          <Alert tone="warning">会话已过期，请重新登录</Alert>
        </div>
      ) : null}

      <h2>登录</h2>
      <p className="sub">使用账号登录控制台。</p>

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
          icon="mail"
          placeholder="name@example.com"
          autoComplete="username"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />

        <div className="field">
          <div className="field-row">
            <label className="field-label" htmlFor="login-password">
              密码
            </label>
            <Link className="app-inline-link" to="/forgot-password">
              忘记密码？
            </Link>
          </div>
          <div className="input-icon">
            <span className="ico">
              <Icon name="shield" />
            </span>
            <input
              id="login-password"
              className="input"
              type="password"
              placeholder="••••••••"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
        </div>

        {mutation.isError ? (
          <div className="app-alert-block">
            <Alert tone="danger">{loginErrorMessage(mutation.error)}</Alert>
          </div>
        ) : null}

        <button
          className="btn btn--primary auth-submit"
          type="submit"
          disabled={pending}
          aria-busy={pending}
        >
          {pending ? '登录中…' : '登录控制台'}
          <Icon name="arrowRight" />
        </button>
      </form>
    </div>
  )
}
