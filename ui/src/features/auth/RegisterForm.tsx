/**
 * **功能**
 * 注册表单：邮箱、口令、确认、协议勾选；接 `POST /api/auth/register`。
 *
 * **归属**
 * `features/auth` 域组件。
 *
 * **口径**
 * 开关、协议、字段任一不合法都**零请求**。
 * 请求体只含契约 `RegisterRequest` 的六个字段；确认口径只在前端比对。
 * 人机验证由 `TurnstileSlot` 预留版面，尚未接入，不参与校验。
 * 协议一个勾选同时置位 `agree_terms` 与 `agree_privacy`。
 * 409 先重拉注册状态：版本变了才算「条款已更新」，否则落到邮箱重复。
 * 成功即建会话：写 `['me']` 缓存后交给页面导航。
 * 展示构建不发请求，走本地延时后按成功处理。
 */

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Icon } from '@/components/ui/Icon'
import { PasswordInput, TextInput } from '@/components/ui/Input'
import {
  getPasswordPolicy,
  getRegistrationStatus,
  register,
  type RegistrationStatus,
  type SessionUser,
} from '@/lib/api/auth'
import { ApiError, describeApiError } from '@/lib/api/client'
import { fieldErrorId } from '@/lib/field-aria'
import { clearSessionExpired, ME_QUERY_KEY, SHOWCASE } from '@/lib/session'
import { cx } from '@/lib/cx'
import {
  buildRegisterBody,
  confirmLiveError,
  firstRegisterField,
  passwordClassList,
  passwordRuleText,
  passwordStrength,
  passwordStrengthLabel,
  registerProblem,
  resolvePolicy,
  validateRegister,
  type RegisterDraft,
  type RegisterErrors,
  type RegisterField,
} from './register'
import { TurnstileSlot } from './TurnstileSlot'
import { REGISTRATION_QUERY_KEY } from './useRegistrationStatus'

type RegisterFormProps = {
  status: RegistrationStatus | undefined
  statusLoading: boolean
  statusError: boolean
  onRetryStatus: () => void
  onSwitchToLogin: () => void
  onRegisterStart?: () => void
  onRegisterFailed?: () => void
  onRegistered: (me: SessionUser | null) => void
}

const EMPTY: RegisterDraft = { email: '', password: '', confirm: '', agree: false }

export function RegisterForm({
  status,
  statusLoading,
  statusError,
  onRetryStatus,
  onSwitchToLogin,
  onRegisterStart,
  onRegisterFailed,
  onRegistered,
}: RegisterFormProps) {
  const queryClient = useQueryClient()
  // 契约里 policy 需登录：公开注册页未登录不发请求，
  // 否则其 401 被全局回调判成会话过期踢回登录页；未登录走本地兜底。
  // 读缓存而非再挂一个 useMe：避免给 auth 域新增模块依赖边。
  const signedIn = Boolean(queryClient.getQueryData(ME_QUERY_KEY))
  // 口令策略以服务端为唯一权威；读不到落本地兜底，不阻断注册。
  const policyQuery = useQuery({
    queryKey: ['password-policy'],
    queryFn: getPasswordPolicy,
    retry: false,
    staleTime: 300_000,
    enabled: signedIn,
  })
  const policy = resolvePolicy(policyQuery.data ?? null)
  const [draft, setDraft] = useState<RegisterDraft>(EMPTY)
  const [errors, setErrors] = useState<RegisterErrors>({})
  const [problem, setProblem] = useState<string | null>(null)
  const problemRef = useRef<HTMLDivElement>(null)
  const submittedRef = useRef<{ termsVersion: number; privacyVersion: number } | null>(null)

  const mutation = useMutation({
    mutationFn: async (body: ReturnType<typeof buildRegisterBody>) => {
      if (SHOWCASE) {
        await new Promise((resolve) => setTimeout(resolve, 400))
        return null
      }
      return register(body)
    },
    onSuccess: (me) => {
      clearSessionExpired()
      if (me) {
        queryClient.setQueryData(ME_QUERY_KEY, me)
        void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY })
      }
      onRegistered(me)
    },
    onError: async (error) => {
      onRegisterFailed?.()
      const submitted = submittedRef.current
      if (error instanceof ApiError && error.status === 409 && submitted) {
        // 409 有两种来源：邮箱重复、条款版本过期。重拉状态后再判。
        try {
          const fresh = await queryClient.fetchQuery({
            queryKey: REGISTRATION_QUERY_KEY,
            queryFn: getRegistrationStatus,
            staleTime: 0,
          })
          queryClient.setQueryData(REGISTRATION_QUERY_KEY, fresh)
          const mapped = registerProblem(error, submitted, fresh)
          if (mapped.field) {
            setErrors((prev) => ({ ...prev, [mapped.field!]: mapped.message }))
            focusField(mapped.field)
          } else {
            if (fresh.terms_version !== submitted.termsVersion || fresh.privacy_version !== submitted.privacyVersion) {
              setDraft((prev) => ({ ...prev, agree: false }))
            }
            setProblem(mapped.message)
          }
          return
        } catch {
          // 状态读不到就按通用冲突展示，不猜是邮箱重复还是条款过期。
          setProblem('提交冲突，请刷新页面后重试。')
          return
        }
      }
      if (error instanceof ApiError && error.status === 403) {
        void queryClient.invalidateQueries({ queryKey: REGISTRATION_QUERY_KEY })
      }
      setProblem(
        submitted
          ? registerProblem(error, submitted, status ?? null).message
          : error instanceof ApiError
            ? describeApiError(error)
            : '网络异常，请稍后重试',
      )
    },
  })

  const pending = mutation.isPending

  useEffect(() => {
    if (problem !== null) problemRef.current?.focus()
  }, [problem])

  function focusField(field: RegisterField | undefined): void {
    if (!field) return
    const id = field === 'agree' ? 'register-agree' : `register-${field}`
    document.getElementById(id)?.focus()
  }

  function change(patch: Partial<RegisterDraft>): void {
    setDraft((prev) => ({ ...prev, ...patch }))
    setProblem(null)
    if (mutation.isError) mutation.reset()
  }

  function submit(): void {
    if (pending) return
    const next = validateRegister(draft, status, policy)
    setErrors(next)
    setProblem(null)
    const first = firstRegisterField(next)
    if (first !== null) {
      focusField(first)
      return
    }
    if (!status) return
    submittedRef.current = {
      termsVersion: status.terms_version,
      privacyVersion: status.privacy_version,
    }
    onRegisterStart?.()
    mutation.mutate(buildRegisterBody(draft, status))
  }

  if (statusLoading) {
    return (
      <>
        <h2>创建账号</h2>
        <p className="sub">正在读取注册状态…</p>
        <div className="app-skeleton-lines" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
      </>
    )
  }

  if (statusError) {
    return (
      <>
        <h2>创建账号</h2>
        <Alert tone="danger" title="注册状态读取失败">
          暂时无法确认是否开放注册。请稍后重试。
        </Alert>
        <div className="auth-form">
          <button type="button" className="btn btn--secondary auth-submit" onClick={onRetryStatus}>
            重试
          </button>
          <button type="button" className="btn btn--ghost auth-submit" onClick={onSwitchToLogin}>
            返回登录
          </button>
        </div>
      </>
    )
  }

  if (status && !status.enabled) {
    return (
      <>
        <h2>创建账号</h2>
        <Alert tone="info" title="当前未开放自助注册">
          请联系管理员开通账号。若你已有账号，可以直接登录。
        </Alert>
        <div className="auth-form">
          <button type="button" className="btn btn--primary auth-submit" onClick={onSwitchToLogin}>
            返回登录
          </button>
        </div>
      </>
    )
  }

  const strength = passwordStrength(draft.password, policy)
  const strengthLabel = passwordStrengthLabel(strength.score)
  const lengthOk = draft.password.length >= policy.minChars
  const classList = passwordClassList(draft.password)
  const classChips = [
    { key: 'upper', label: '大写', ok: classList.upper },
    { key: 'lower', label: '小写', ok: classList.lower },
    { key: 'digit', label: '数字', ok: classList.digit },
    { key: 'symbol', label: '符号', ok: classList.symbol },
  ] as const
  const hasPassword = draft.password !== ''
  const confirmMatched = draft.confirm !== '' && draft.confirm === draft.password

  return (
    <>
      <h2>创建账号</h2>
      <p className="sub">注册后可绑定易班账号，按你的时间偏好自动签到。</p>

      <form
        className="auth-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <TextInput
          id="register-email"
          label="邮箱"
          type="email"
          name="email"
          inputMode="email"
          icon="mail"
          placeholder="name@example.com"
          autoComplete="email"
          required
          value={draft.email}
          error={errors.email}
          onChange={(event) => change({ email: event.target.value })}
        />

        <PasswordInput
          id="register-password"
          label="密码"
          name="password"
          placeholder="设置登录密码"
          autoComplete="new-password"
          required
          value={draft.password}
          error={errors.password}
          hint={hasPassword ? undefined : passwordRuleText(policy)}
          onChange={(event) => change({ password: event.target.value })}
        />

        {/* 强度模块：输入后才出现（空态由上方 hint 文本承载规则）。
            分段条按 score 点亮换语义色；四类字符 chips 逐键反馈。 */}
        {hasPassword ? (
          <div className="app-pw-meter" data-score={strength.score} aria-live="polite">
            <div className="pw-strength">
              <div
                className="pw-bar"
                role="img"
                aria-label={`密码强度：${strengthLabel}`}
              >
                <span className="pw-seg" />
                <span className="pw-seg" />
                <span className="pw-seg" />
                <span className="pw-seg" />
              </div>
              <span className="pw-word">{strengthLabel}</span>
            </div>
            <div className="pw-reqs">
              <span className={cx('pw-chip', lengthOk && 'is-ok')}>
                <Icon name={lengthOk ? 'check' : 'trendFlat'} size={11} />
                <span>至少 {policy.minChars} 位</span>
              </span>
              {classChips.map((chip) => (
                <span key={chip.key} className={cx('pw-chip', chip.ok && 'is-ok')}>
                  <Icon name={chip.ok ? 'check' : 'trendFlat'} size={11} />
                  {chip.label}
                </span>
              ))}
            </div>
          </div>
        ) : null}

        <PasswordInput
          id="register-confirm"
          label="确认密码"
          name="confirm"
          placeholder="再次输入密码"
          autoComplete="new-password"
          required
          value={draft.confirm}
          error={errors.confirm ?? confirmLiveError(draft)}
          success={confirmMatched ? '已完成' : undefined}
          onChange={(event) => change({ confirm: event.target.value })}
        />

        <div className="field">
          <label className="check app-register-agree">
            <input
              id="register-agree"
              type="checkbox"
              checked={draft.agree}
              aria-invalid={errors.agree ? true : undefined}
              aria-describedby={errors.agree ? fieldErrorId('register-agree') : undefined}
              onChange={(event) => change({ agree: event.target.checked })}
            />
            <span className="box" aria-hidden="true" />
            <span>
              我已阅读并同意
              <a
                className="app-inline-link"
                href="/terms"
                target="_blank"
                rel="noopener noreferrer"
              >
                《用户协议》
              </a>
              和
              <a
                className="app-inline-link"
                href="/privacy"
                target="_blank"
                rel="noopener noreferrer"
              >
                《隐私政策》
              </a>
            </span>
          </label>
          {errors.agree ? (
            <div className="field-error" id={fieldErrorId('register-agree')} role="alert">
              <Icon name="alertCircle" size={11} />
              {errors.agree}
            </div>
          ) : null}
        </div>

        {problem ? (
          <div className="app-alert-block" ref={problemRef} tabIndex={-1}>
            <Alert tone="danger">{problem}</Alert>
          </div>
        ) : null}

        <TurnstileSlot />

          <button
            className="btn btn--primary auth-submit"
            type="submit"
            disabled={pending}
            aria-busy={pending}
          >
            {pending ? '创建中…' : '创建账号'}
          </button>
      </form>

      <p className="auth-switch-hint">
        已有账号？
        <button type="button" className="app-inline-link" onClick={onSwitchToLogin}>
          返回登录
        </button>
      </p>
    </>
  )
}
