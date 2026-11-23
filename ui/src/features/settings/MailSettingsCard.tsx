/**
 * **功能**
 * 邮件（SMTP）设置卡：读取生效配置、编辑主机/端口/加密方式/发件人/账号/密码、
 * 带本人登录口令保存，并向本人发一封测试邮件。
 *
 * **归属**
 * `features/settings` 展示层：校验与补丁拼装在 `mail-settings.ts`，请求在 `lib/api/mail.ts`。
 *
 * **口径**
 * - 读接口归 master/admin，写接口只归 master：非主管理员看到同一张卡的只读版本，
 *   字段全部置灰并给出原因，绝不发出写请求。
 * - 三项敏感值（发件人/账号/密码）服务端永不回显：输入框恒为空，留空＝不修改，
 *   只有填写才进补丁；密码不进任何缓存、日志或 URL。
 * - 部分补丁：非敏感项与当前生效值相同就不提交，没有改动时保存按钮置灰。
 * - 两次写（保存、发测试邮件）都必带操作者本人 `current_password`；
 *   成功后清空口令，失败错误的信封原样留在卡内，不改 toast。
 * - 展示构建读合成夹具、零网络请求，全部写入口置灰并标明「展示模式只读」。
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { AsyncState } from '@/components/ui/AsyncState'
import { BusyButton } from '@/components/ui/BusyButton'
import { Card } from '@/components/ui/Card'
import { PasswordInput, SelectInput, TextInput } from '@/components/ui/Input'
import { notifySuccess } from '@/components/ui/toast'
import {
  MAIL_SETTINGS_QUERY_KEY,
  getMailSettings,
  sendTestMail,
  updateMailSettings,
  type MailSettingsReport,
} from '@/lib/api/mail'
import { showcaseMailSettings } from '@/lib/showcase'
import { READ_ONLY_REASON, SHOWCASE } from '@/lib/showcase-mode'
import {
  MAIL_SAVED_TOAST,
  MAIL_TLS_OPTIONS,
  MAIL_TEST_TOAST,
  buildMailSettingsUpdate,
  mailDraftFieldErrors,
  mailPasswordText,
  mailSensitiveText,
  mailSettingsDraft,
  mailSettingsErrorMessage,
  mailSourceLabel,
  mailTestErrorMessage,
  type MailDraftField,
  type MailSettingsDraft,
} from './mail-settings'

type MailSettingsCardProps = {
  /** GET 归 master/admin，PUT 只归 master：其余角色看只读版本。 */
  canWrite: boolean
  className?: string
}

function sourceNote(report: MailSettingsReport, key: 'host' | 'port' | 'tls_mode' | 'from' | 'user' | 'pass'): string {
  return `来源：${mailSourceLabel(report[key].source)}`
}

export function MailSettingsCard({ canWrite, className }: MailSettingsCardProps) {
  const queryClient = useQueryClient()
  const readOnlyReason = SHOWCASE ? READ_ONLY_REASON : canWrite ? null : '仅主管理员可修改 SMTP 设置'
  const [draft, setDraft] = useState<MailSettingsDraft | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<MailDraftField, string>>>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ tone: 'success' | 'danger'; text: string } | null>(null)

  const query = useQuery({
    queryKey: MAIL_SETTINGS_QUERY_KEY,
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseMailSettings()) : getMailSettings()),
    staleTime: SHOWCASE ? Infinity : 30_000,
    retry: false,
  })

  const report = query.data
  const form = draft ?? mailSettingsDraft(report)

  const patch = (next: Partial<MailSettingsDraft>) => {
    setDraft({ ...form, ...next })
    setSubmitError(null)
    setTestResult(null)
  }

  const saveMutation = useMutation({
    mutationFn: async (input: { report: MailSettingsReport; draft: MailSettingsDraft; password: string }) => {
      const body = buildMailSettingsUpdate(input.report, input.draft, input.password)
      if (!body) throw new Error('没有需要保存的改动')
      return updateMailSettings(body)
    },
    onSuccess: (next) => {
      setFieldErrors({})
      setSubmitError(null)
      setTestResult(null)
      // 口令是一次性的：写成功后立刻从内存里抹掉。
      setDraft(mailSettingsDraft(next))
      queryClient.setQueryData(MAIL_SETTINGS_QUERY_KEY, next)
      void queryClient.invalidateQueries({ queryKey: MAIL_SETTINGS_QUERY_KEY })
      notifySuccess(MAIL_SAVED_TOAST)
    },
    onError: (error) => {
      if (error instanceof Error && error.message === '没有需要保存的改动') {
        setSubmitError('没有需要保存的改动：请先修改至少一项设置')
        return
      }
      setSubmitError(mailSettingsErrorMessage(error))
    },
  })

  const testMutation = useMutation({
    mutationFn: (password: string) => sendTestMail({ current_password: password }),
    onSuccess: (result) => {
      setSubmitError(null)
      setTestResult({ tone: 'success', text: `测试邮件已提交：${result.message}` })
      // 与保存一样，口令不留在表单里。
      setDraft({ ...form, currentPassword: '' })
      notifySuccess(MAIL_TEST_TOAST)
    },
    onError: (error) => {
      setSubmitError(null)
      setTestResult({ tone: 'danger', text: mailTestErrorMessage(error) })
    },
  })

  const pending = saveMutation.isPending || testMutation.isPending

  const submit = () => {
    if (!report) return
    const errors = mailDraftFieldErrors(form)
    setFieldErrors(errors)
    setSubmitError(null)
    setTestResult(null)
    if (Object.keys(errors).length > 0) return
    saveMutation.mutate({ report, draft: form, password: form.currentPassword })
  }

  const sendTest = () => {
    if (form.currentPassword.trim() === '') {
      setFieldErrors({ currentPassword: '请输入你的登录口令' })
      setSubmitError(null)
      return
    }
    setFieldErrors({})
    setSubmitError(null)
    testMutation.mutate(form.currentPassword)
  }

  const dirty =
    report !== undefined &&
    buildMailSettingsUpdate(report, { ...form, currentPassword: 'x' }, 'x') !== null

  const status = query.isPending
    ? 'loading'
    : query.isError
      ? 'error'
      : report === undefined
        ? 'empty'
        : 'data'

  return (
    <Card
      className={className}
      eyebrow="Mail"
      title="邮件（SMTP）设置"
      action={
        <span className="app-caption">
          {readOnlyReason ?? '保存与发测试邮件都要复核你的登录口令'}
        </span>
      }
    >
      <AsyncState
        status={status}
        loadingLabel="读取邮件设置…"
        emptyLabel="暂无邮件设置"
        error={query.isError ? mailSettingsErrorMessage(query.error) : undefined}
        onRetry={() => {
          void query.refetch()
        }}
      >
        {report ? (
          <form
            noValidate
            className="app-form-stack"
            onSubmit={(event) => {
              event.preventDefault()
              if (readOnlyReason !== null || pending) return
              submit()
            }}
          >
            <div className="s-mail-grid">
              <TextInput
                id="mail-host"
                label="SMTP 主机"
                hint={`必填。${sourceNote(report, 'host')}`}
                value={form.host}
                error={fieldErrors.host}
                disabled={readOnlyReason !== null}
                autoComplete="off"
                onChange={(event) => patch({ host: event.target.value })}
              />

              <TextInput
                id="mail-port"
                label="端口"
                inputMode="numeric"
                hint={`1 到 65535。${sourceNote(report, 'port')}`}
                value={form.port}
                error={fieldErrors.port}
                disabled={readOnlyReason !== null}
                autoComplete="off"
                onChange={(event) => patch({ port: event.target.value })}
              />

              <SelectInput
                id="mail-tls"
                label="加密方式"
                hint={`契约只接受小写枚举值。${sourceNote(report, 'tls_mode')}`}
                options={MAIL_TLS_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
                value={form.tlsMode}
                disabled={readOnlyReason !== null}
                onChange={(event) => patch({ tlsMode: event.target.value })}
              />

              <TextInput
                id="mail-from"
                label="发件人地址"
                hint={`服务端不回显原值，留空＝不修改。当前 ${mailSensitiveText(report.from)}；${sourceNote(report, 'from')}`}
                placeholder="留空＝不修改"
                value={form.from}
                error={fieldErrors.from}
                disabled={readOnlyReason !== null}
                autoComplete="off"
                onChange={(event) => patch({ from: event.target.value })}
              />

              <TextInput
                id="mail-user"
                label="SMTP 账号"
                hint={`服务端不回显原值，留空＝不修改。当前 ${mailSensitiveText(report.user)}；${sourceNote(report, 'user')}`}
                placeholder="留空＝不修改"
                value={form.user}
                error={fieldErrors.user}
                disabled={readOnlyReason !== null}
                autoComplete="off"
                onChange={(event) => patch({ user: event.target.value })}
              />

              <PasswordInput
                id="mail-pass"
                label="SMTP 密码"
                hint={`加密存储且永不回显，留空＝不修改。当前 ${mailPasswordText(report.pass)}；${sourceNote(report, 'pass')}`}
                placeholder="留空＝不修改"
                value={form.pass}
                error={fieldErrors.pass}
                disabled={readOnlyReason !== null}
                autoComplete="new-password"
                onChange={(event) => patch({ pass: event.target.value })}
              />

              <div className="s-mail-grid-wide">
                <PasswordInput
                  id="mail-current-password"
                  label="你的登录口令"
                  hint="用于本次保存或测试邮件的二次确认；口令不会保存，也不会出现在任何记录里。"
                  name="current_password"
                  autoComplete="current-password"
                  value={form.currentPassword}
                  error={fieldErrors.currentPassword}
                  disabled={readOnlyReason !== null}
                  onChange={(event) => patch({ currentPassword: event.target.value })}
                />
              </div>
            </div>

            <div className="app-page-actions">
              <BusyButton
                variant="outline-primary"
                type="button"
                busy={testMutation.isPending}
                busyLabel="发送中…"
                disabled={readOnlyReason !== null || pending}
                title={readOnlyReason ?? undefined}
                onClick={sendTest}
              >
                发送测试邮件
              </BusyButton>
              <BusyButton
                variant="primary"
                type="submit"
                busy={saveMutation.isPending}
                busyLabel="保存中…"
                disabled={readOnlyReason !== null || pending || !dirty}
                title={readOnlyReason ?? (dirty ? undefined : '没有需要保存的改动')}
              >
                保存设置
              </BusyButton>
              {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
            </div>

            {submitError ? (
              <Alert tone="danger" title="保存失败">
                {submitError}
              </Alert>
            ) : null}

            {testResult ? <Alert tone={testResult.tone}>{testResult.text}</Alert> : null}

            <p className="app-muted">
              收件人固定为当前登录的主管理员本人，无法指定；连续复核失败会短暂限流。
              三项敏感值只在本表单内提交一次，服务端只保留加密后的密文。
            </p>
          </form>
        ) : null}
      </AsyncState>
    </Card>
  )
}
