/**
 * **功能**
 * 全局容量配额卡（max_users / max_accounts），支持 0=不限 与恢复默认(1)。
 *
 * **归属**
 * `features/system/policy` 子组件。
 *
 * **口径**
 * - 两个字段均接收 [0, 100000] 整数；0 表示跳过总数上限检查，每人最多绑定一个启用号仍独立生效。
 * - 默认值为 1（紧档）；点击「恢复默认 (1)」显式传 null 删除覆盖行。
 */

import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { AsyncState } from '@/components/ui/AsyncState'
import { Badge } from '@/components/ui/Badge'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { TextInput } from '@/components/ui/Input'
import { notifySuccess } from '@/components/ui/toast'
import {
  getQuotaSettings,
  QUOTA_SETTINGS_QUERY_KEY,
  updateQuotaSettings,
  type AdminQuotaSettingsUpdate,
} from '@/lib/api/adminSettings'
import { showcaseQuotaSettings } from '@/lib/showcase'
import { SHOWCASE } from '@/lib/showcase-mode'
import {
  buildQuotaResetPatch,
  buildQuotaSettingsPatch,
  createQuotaPolicyDraft,
  formatSystemWriteError,
  settingSourceLabel,
  validateQuotaPolicyDraft,
  type QuotaPolicyDraft,
} from './drafts'
import type { PolicyCardAccessProps } from './PolicySwitchesCards'

export function PolicyQuotaCard({ canWrite, readOnlyReason }: PolicyCardAccessProps) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<QuotaPolicyDraft | null>(null)
  const [touched, setTouched] = useState<{ maxUsers?: boolean; maxAccounts?: boolean }>({})
  const [submitted, setSubmitted] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [statusNote, setStatusNote] = useState<string | null>(null)

  const query = useQuery({
    queryKey: QUOTA_SETTINGS_QUERY_KEY,
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseQuotaSettings()) : getQuotaSettings()),
    staleTime: 30_000,
  })

  const mutation = useMutation({
    mutationFn: (body: AdminQuotaSettingsUpdate) => updateQuotaSettings(body),
    onSuccess: (next) => {
      setSubmitError(null)
      setStatusNote(null)
      setDraft(null)
      setTouched({})
      setSubmitted(false)
      queryClient.setQueryData(QUOTA_SETTINGS_QUERY_KEY, next)
      void queryClient.invalidateQueries({ queryKey: QUOTA_SETTINGS_QUERY_KEY })
      notifySuccess('容量配额已保存')
    },
    onError: (err) => {
      setStatusNote(null)
      setSubmitError(formatSystemWriteError(err))
    },
  })

  const initialDraft = query.data ? createQuotaPolicyDraft(query.data) : null
  const effectiveDraft = draft ?? initialDraft
  const isDirty = Boolean(
    initialDraft &&
      effectiveDraft &&
      (effectiveDraft.maxUsers !== initialDraft.maxUsers ||
        effectiveDraft.maxAccounts !== initialDraft.maxAccounts),
  )
  const rawErrors = effectiveDraft ? validateQuotaPolicyDraft(effectiveDraft) : {}
  const shownErrors = {
    maxUsers: submitted || touched.maxUsers ? rawErrors.maxUsers : undefined,
    maxAccounts: submitted || touched.maxAccounts ? rawErrors.maxAccounts : undefined,
  }

  function updateField(key: keyof QuotaPolicyDraft, value: string) {
    if (!effectiveDraft) return
    setSubmitError(null)
    setStatusNote(null)
    setDraft({ ...effectiveDraft, [key]: value })
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canWrite || mutation.isPending || !query.data || !effectiveDraft) return
    setSubmitted(true)
    setSubmitError(null)
    setStatusNote(null)
    const errors = validateQuotaPolicyDraft(effectiveDraft)
    if (errors.maxUsers) {
      document.getElementById('policy-quota-users')?.focus()
      return
    }
    if (errors.maxAccounts) {
      document.getElementById('policy-quota-accounts')?.focus()
      return
    }
    const patch = buildQuotaSettingsPatch(effectiveDraft, query.data)
    if (!patch) {
      setStatusNote('当前配额未发生变化')
      return
    }
    mutation.mutate(patch)
  }

  function handleReset() {
    if (!canWrite || mutation.isPending || !query.data) return
    setSubmitError(null)
    setStatusNote(null)
    mutation.mutate(buildQuotaResetPatch())
  }

  const asyncStatus = query.isPending ? 'loading' : query.isError ? 'error' : 'data'
  const hasDbOverride = Boolean(
    query.data &&
      (query.data.max_users.source === 'db' || query.data.max_accounts.source === 'db'),
  )

  return (
    <Card
      id="policy-quota-card"
      eyebrow="Quota"
      title="容量配额"
      className="sys-policy-card sys-policy-card-wide"
    >
      <p className="sys-policy-desc">
        控制全站活跃用户与启用签到账号数量上限；设为 0 表示不限制总量，默认紧档为 1。
      </p>
      <AsyncState
        status={asyncStatus}
        error={query.isError ? formatSystemWriteError(query.error) : undefined}
        onRetry={() => void query.refetch()}
      >
        {query.data && effectiveDraft ? (
          <form
            className="app-form-stack"
            onSubmit={handleSubmit}
            aria-labelledby="policy-quota-heading"
            noValidate
          >
            <span id="policy-quota-heading" className="app-visually-hidden">
              容量配额
            </span>

            <div className="sys-quota-fields">
              <div className="sys-field-block">
                <div className="sys-source-row">
                  <span className="sys-source-key">用户上限来源</span>
                  <Badge tone={query.data.max_users.source === 'db' ? 'info' : undefined}>
                    {settingSourceLabel(query.data.max_users.source)}
                  </Badge>
                </div>
                <TextInput
                  id="policy-quota-users"
                  label="全站活跃用户上限"
                  inputMode="numeric"
                  required
                  disabled={!canWrite || mutation.isPending}
                  value={effectiveDraft.maxUsers}
                  hint="0 表示不限；范围 0–100000，默认 1"
                  error={shownErrors.maxUsers}
                  onChange={(e) => updateField('maxUsers', e.target.value)}
                  onBlur={() => setTouched((prev) => ({ ...prev, maxUsers: true }))}
                />
              </div>

              <div className="sys-field-block">
                <div className="sys-source-row">
                  <span className="sys-source-key">账号上限来源</span>
                  <Badge tone={query.data.max_accounts.source === 'db' ? 'info' : undefined}>
                    {settingSourceLabel(query.data.max_accounts.source)}
                  </Badge>
                </div>
                <TextInput
                  id="policy-quota-accounts"
                  label="全站启用账号上限"
                  inputMode="numeric"
                  required
                  disabled={!canWrite || mutation.isPending}
                  value={effectiveDraft.maxAccounts}
                  hint="0 表示不限（仍保留一人一主号约束）；范围 0–100000，默认 1"
                  error={shownErrors.maxAccounts}
                  onChange={(e) => updateField('maxAccounts', e.target.value)}
                  onBlur={() => setTouched((prev) => ({ ...prev, maxAccounts: true }))}
                />
              </div>
            </div>

            {submitError ? <Alert tone="danger">{submitError}</Alert> : null}
            {statusNote ? (
              <p className="sys-status-note" role="status">
                {statusNote}
              </p>
            ) : null}

            <div className="sys-card-actions">
              {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
              {canWrite && isDirty && !mutation.isPending ? (
                <span className="sys-dirty-note" role="status">
                  有未保存变更
                </span>
              ) : null}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={!canWrite || mutation.isPending || !hasDbOverride}
                title={readOnlyReason ?? undefined}
                onClick={handleReset}
              >
                恢复默认 (1)
              </Button>
              <BusyButton
                type="submit"
                variant="primary"
                size="sm"
                busy={mutation.isPending}
                busyLabel="保存中…"
                disabled={!canWrite}
                title={readOnlyReason ?? undefined}
              >
                保存配额
              </BusyButton>
            </div>
          </form>
        ) : null}
      </AsyncState>
    </Card>
  )
}
