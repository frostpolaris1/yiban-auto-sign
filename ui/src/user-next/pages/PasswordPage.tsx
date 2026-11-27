/**
 * **功能**
 * 用户端「修改密码」页：按服务端策略校验，提交后回到我的。
 *
 * **归属**
 * user-next/pages；策略与会话动作复用 lib/api/auth。
 *
 * **口径**
 * 规则来自 GET /api/auth/password-policy，不硬编码。
 * 口令不留在页面关闭后的状态里；401 先经 me 探针定论。
 */

import { useState } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { notifySuccess } from '@/components/ui/toast'
import {
  changePassword,
  getPasswordPolicy,
  SessionExpiredError,
  type SessionUser,
} from '@/lib/api/auth'
import { ApiError } from '@/lib/api/client'
import { expireSession, ME_QUERY_KEY, useMe } from '@/lib/session'

function classCount(value: string): number {
  return (
    Number(/[A-Z]/.test(value)) +
    Number(/[a-z]/.test(value)) +
    Number(/[0-9]/.test(value)) +
    Number(/[^A-Za-z0-9]/.test(value))
  )
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).length
}

export function PasswordPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const me = useMe()
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
  const [currentError, setCurrentError] = useState<string | null>(null)
  const [attempted, setAttempted] = useState(false)
  const [showCurrent, setShowCurrent] = useState(false)
  const [showNext, setShowNext] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)

  const policy = policyQuery.data
  const classes = classCount(next)
  const charCount = Array.from(next).length
  const lengthOk = policy ? charCount >= policy.min_chars : false
  const classesOk = policy ? classes >= policy.min_classes : false
  const bytesOk = policy ? byteLength(next) <= policy.max_bytes : false
  const complete = current !== '' && next !== '' && confirm !== ''

  const mutation = useMutation({
    mutationFn: () => changePassword({ current_password: current, new_password: next }),
    onSuccess: () => {
      queryClient.setQueryData<SessionUser>(ME_QUERY_KEY, (previous) =>
        previous ? { ...previous, must_change_password: false } : previous,
      )
      notifySuccess('密码已修改')
      void navigate({ to: '/app/me', replace: true })
    },
    onError: (err) => {
      if (err instanceof SessionExpiredError) {
        expireSession((options) => void navigate(options))
        return
      }
      if (err instanceof ApiError) {
        if (err.status === 401) {
          setCurrentError('当前密码不正确')
          return
        }
        if (err.status === 409) {
          setError('新密码不能与当前密码相同。')
          return
        }
        if (err.status === 429) {
          setError('尝试过多，请稍后再试。')
          return
        }
        if (err.status === 400) {
          setError('新密码不符合规则，请按右侧提示修改。')
          return
        }
      }
      setError('修改失败，请稍后重试。')
    },
  })

  const submit = () => {
    setError(null)
    setCurrentError(null)
    setAttempted(true)
    if (!policy) {
      setError('密码规则还没读到，请重试。')
      return
    }
    if (current === '') {
      setCurrentError('请输入当前密码。')
      document.getElementById('un-pw-current')?.focus()
      return
    }
    if (!bytesOk) {
      setError('新密码超出系统长度上限，请精简后再试。')
      document.getElementById('un-pw-next')?.focus()
      return
    }
    if (!lengthOk || !classesOk) {
      setError('新密码不符合规则，请按右侧提示修改。')
      document.getElementById('un-pw-next')?.focus()
      return
    }
    if (next !== confirm) {
      setError('两次输入的新密码不一致。')
      document.getElementById('un-pw-confirm')?.focus()
      return
    }
    if (next === current) {
      setError('新密码不能与当前密码相同。')
      document.getElementById('un-pw-next')?.focus()
      return
    }
    mutation.mutate()
  }

  return (
    <div className="un-security-page">
      <header className="un-security-page__head">
        <Link to="/app/me" className="un-page-back">
          <span aria-hidden="true">←</span> 返回我的
        </Link>
        <h1 className="un-security-page__title">修改密码</h1>
        <p className="un-security-page__lede">
          修改后，下次登录请使用新密码。易班口令和本站密码互不影响。
        </p>
      </header>

      {me.data?.must_change_password ? (
        <p className="un-alert un-alert--warning">当前账号需要先完成密码修改，才能继续使用。</p>
      ) : null}

      <div className="un-security-page__layout">
        <form
          className="un-security-page__form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            if (!mutation.isPending) submit()
          }}
        >
          {error ? (
            <p className="un-alert un-alert--danger" role="alert">
              {error}
            </p>
          ) : null}
          {policyQuery.isError ? (
            <p className="un-alert un-alert--danger" role="alert">
              密码规则读取失败，暂时不能修改。
              <button type="button" className="un-link" onClick={() => void policyQuery.refetch()}>
                重试
              </button>
            </p>
          ) : null}

          <div className="un-field">
            <label className="un-field__label" htmlFor="un-pw-current">当前密码</label>
            <div className="un-field__control">
              <input
                id="un-pw-current"
                className={'un-input' + (currentError ? ' has-error' : '')}
                type={showCurrent ? 'text' : 'password'}
                autoComplete="current-password"
                value={current}
                aria-invalid={currentError ? true : undefined}
                aria-describedby={currentError ? 'un-pw-current-error' : undefined}
                onChange={(event) => {
                  setCurrent(event.target.value)
                  setCurrentError(null)
                  setError(null)
                }}
              />
              <button
                type="button"
                className="un-field__reveal"
                aria-label={showCurrent ? '隐藏当前密码' : '显示当前密码'}
                aria-pressed={showCurrent}
                onClick={() => setShowCurrent((value) => !value)}
              >
                {showCurrent ? '隐藏' : '显示'}
              </button>
            </div>
            {currentError ? (
              <p id="un-pw-current-error" className="un-field__error" role="alert">
                {currentError}
              </p>
            ) : null}
          </div>

          <div className="un-field">
            <label className="un-field__label" htmlFor="un-pw-next">新密码</label>
            <div className="un-field__control">
              <input
                id="un-pw-next"
                className={
                  'un-input' +
                  (attempted && (!lengthOk || !classesOk || !bytesOk) ? ' has-error' : '')
                }
                type={showNext ? 'text' : 'password'}
                autoComplete="new-password"
                value={next}
                aria-invalid={attempted && (!lengthOk || !classesOk || !bytesOk) ? true : undefined}
                aria-describedby={policy ? 'un-pw-policy' : undefined}
                onChange={(event) => {
                  setNext(event.target.value)
                  setError(null)
                }}
              />
              <button
                type="button"
                className="un-field__reveal"
                aria-label={showNext ? '隐藏新密码' : '显示新密码'}
                aria-pressed={showNext}
                onClick={() => setShowNext((value) => !value)}
              >
                {showNext ? '隐藏' : '显示'}
              </button>
            </div>
          </div>

          <div className="un-field">
            <label className="un-field__label" htmlFor="un-pw-confirm">确认新密码</label>
            <div className="un-field__control">
              <input
                id="un-pw-confirm"
                className="un-input"
                type={showConfirm ? 'text' : 'password'}
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => {
                  setConfirm(event.target.value)
                  setError(null)
                }}
              />
              <button
                type="button"
                className="un-field__reveal"
                aria-label={showConfirm ? '隐藏确认密码' : '显示确认密码'}
                aria-pressed={showConfirm}
                onClick={() => setShowConfirm((value) => !value)}
              >
                {showConfirm ? '隐藏' : '显示'}
              </button>
            </div>
          </div>

          <div className="un-security-page__actions">
            <button
              type="submit"
              className="un-btn un-btn--primary"
              disabled={mutation.isPending || !policy || !complete}
            >
              {mutation.isPending ? '保存中…' : '保存新密码'}
            </button>
            <Link to="/app/me" className="un-btn un-btn--ghost">取消</Link>
          </div>
        </form>

        <aside className="un-security-page__policy" aria-label="密码规则">
          <h2 className="un-security-page__policy-title">密码规则</h2>
          {policy ? (
            <ul id="un-pw-policy" className="un-security-page__checks">
              <li className={lengthOk ? 'is-ok' : attempted ? 'is-bad' : ''}>
                至少 {policy.min_chars} 个字符（当前 {charCount}）
                <span className="un-sr-only">{lengthOk ? '，已满足' : '，尚未满足'}</span>
              </li>
              <li className={classesOk ? 'is-ok' : attempted ? 'is-bad' : ''}>
                至少 {policy.min_classes} 类字符（大写、小写、数字、符号；空白算符号）
                <span className="un-security-page__count">{classes}/{policy.min_classes}</span>
                <span className="un-sr-only">{classesOk ? '，已满足' : '，尚未满足'}</span>
              </li>
              <li className={bytesOk ? 'is-ok' : attempted ? 'is-bad' : ''}>
                不超过 {policy.max_bytes} 字节（当前 {byteLength(next)}）
                <span className="un-sr-only">{bytesOk ? '，已满足' : '，尚未满足'}</span>
              </li>
            </ul>
          ) : (
            <p className="un-security-page__quiet">正在读取规则…</p>
          )}
          <p className="un-security-page__quiet">
            如果忘记本站密码，可以在登录页走邮箱重置；不要用易班口令代替。
          </p>
        </aside>
      </div>
    </div>
  )
}
