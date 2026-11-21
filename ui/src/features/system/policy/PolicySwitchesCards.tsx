/**
 * **功能**
 * 运行策略区两张全局开关卡：公开注册开关与新号自动审核开关。
 *
 * **归属**
 * `features/system/policy` 子组件：复用统一 `PolicyToggleCard` 壳，避免双开关卡片重复逻辑。
 *
 * **口径**
 * 两卡均采用「显式 SwitchControl + 独立保存按钮」形态，防止单点误触即时改写全站策略；
 * 仅主管理员可提交写回，`admin` 与展示模式置灰全部写控件并展示只读原因。
 */

import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient, type QueryKey } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { AsyncState } from '@/components/ui/AsyncState'
import { BusyButton } from '@/components/ui/BusyButton'
import { Card } from '@/components/ui/Card'
import { SwitchControl } from '@/components/ui/SwitchControl'
import { notifySuccess } from '@/components/ui/toast'
import {
  AUTO_APPROVE_QUERY_KEY,
  getAutoApprove,
  getRegistrationStatus,
  REGISTRATION_STATUS_QUERY_KEY,
  updateAutoApprove,
  updateRegistrationStatus,
} from '@/lib/api/adminSettings'
import { showcaseAutoApprove, showcaseRegistrationStatus } from '@/lib/showcase'
import { SHOWCASE } from '@/lib/showcase-mode'
import { formatSystemWriteError } from './drafts'

type ToggleCardProps = {
  cardId: string
  headingId: string
  eyebrow: string
  title: string
  description: string
  queryKey: QueryKey
  fetchEnabled: () => Promise<boolean>
  showcaseEnabled: () => boolean
  saveEnabled: (enabled: boolean) => Promise<boolean>
  switchId: string
  switchLabel: string
  hint: string
  successText: (enabled: boolean) => string
  canWrite: boolean
  readOnlyReason: string | null
}

function PolicyToggleCard({
  cardId,
  headingId,
  eyebrow,
  title,
  description,
  queryKey,
  fetchEnabled,
  showcaseEnabled,
  saveEnabled,
  switchId,
  switchLabel,
  hint,
  successText,
  canWrite,
  readOnlyReason,
}: ToggleCardProps) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)

  const query = useQuery({
    queryKey,
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseEnabled()) : fetchEnabled()),
    staleTime: 30_000,
  })

  const mutation = useMutation({
    mutationFn: saveEnabled,
    onSuccess: (next) => {
      setError(null)
      setDraft(null)
      queryClient.setQueryData(queryKey, next)
      void queryClient.invalidateQueries({ queryKey })
      notifySuccess(successText(next))
    },
    onError: (err) => {
      setError(formatSystemWriteError(err))
    },
  })

  const checked = draft ?? query.data ?? false
  const isDirty = query.data !== undefined && draft !== null && draft !== query.data
  const asyncStatus = query.isPending ? 'loading' : query.isError ? 'error' : 'data'

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canWrite || mutation.isPending || query.data === undefined) return
    setError(null)
    mutation.mutate(checked)
  }

  return (
    <Card id={cardId} eyebrow={eyebrow} title={title} className="sys-policy-card">
      <p className="sys-policy-desc">{description}</p>
      <AsyncState
        status={asyncStatus}
        error={query.isError ? formatSystemWriteError(query.error) : undefined}
        onRetry={() => void query.refetch()}
      >
        <form className="app-form-stack" onSubmit={handleSubmit} aria-labelledby={headingId} noValidate>
          <span id={headingId} className="app-visually-hidden">
            {title}
          </span>
          <div className="sys-switch-field">
            <SwitchControl
              name={switchId}
              label={switchLabel}
              checked={checked}
              disabled={!canWrite || mutation.isPending}
              onCheckedChange={(next) => {
                setError(null)
                setDraft(next)
              }}
            />
            <p className="sys-control-hint">{hint}</p>
          </div>
          {error ? <Alert tone="danger">{error}</Alert> : null}
          <div className="sys-card-actions">
            <BusyButton
              type="submit"
              variant="primary"
              size="sm"
              busy={mutation.isPending}
              busyLabel="保存中…"
              disabled={!canWrite}
              title={readOnlyReason ?? undefined}
            >
              保存设置
            </BusyButton>
            {canWrite && isDirty && !mutation.isPending ? (
              <span className="sys-dirty-note" role="status">
                有未保存变更
              </span>
            ) : null}
            {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
          </div>
        </form>
      </AsyncState>
    </Card>
  )
}

export type PolicyCardAccessProps = {
  canWrite: boolean
  readOnlyReason: string | null
}

export function PolicyRegistrationCard({ canWrite, readOnlyReason }: PolicyCardAccessProps) {
  return (
    <PolicyToggleCard
      cardId="policy-registration-card"
      headingId="policy-registration-heading"
      eyebrow="Access"
      title="注册开关"
      description="控制登录页是否允许新用户自助提交注册申请；关闭后仅可由主管理员创建账号。"
      queryKey={REGISTRATION_STATUS_QUERY_KEY}
      fetchEnabled={async () => (await getRegistrationStatus()).enabled}
      showcaseEnabled={() => showcaseRegistrationStatus().enabled}
      saveEnabled={async (enabled) => (await updateRegistrationStatus({ enabled })).enabled}
      switchId="policy-registration-enabled"
      switchLabel="允许新用户公开注册"
      hint="保存后立即对全站登录与注册入口生效。"
      successText={(enabled) => (enabled ? '已开放公开注册' : '已关闭公开注册')}
      canWrite={canWrite}
      readOnlyReason={readOnlyReason}
    />
  )
}

export function PolicyAutoApproveCard({ canWrite, readOnlyReason }: PolicyCardAccessProps) {
  return (
    <PolicyToggleCard
      cardId="policy-auto-approve-card"
      headingId="policy-auto-approve-heading"
      eyebrow="Review"
      title="自动审核"
      description="控制新提交账号在勾选默认确认后，是否自动核验身份并免人工审核通过。"
      queryKey={AUTO_APPROVE_QUERY_KEY}
      fetchEnabled={async () => (await getAutoApprove()).enabled}
      showcaseEnabled={() => showcaseAutoApprove().enabled}
      saveEnabled={async (enabled) => (await updateAutoApprove({ enabled })).enabled}
      switchId="policy-auto-approve-enabled"
      switchLabel="开启新号自动审核"
      hint="仅影响新创建或重新提交的待审核账号，不改已有账号状态。"
      successText={(enabled) => (enabled ? '已开启自动审核' : '已关闭自动审核')}
      canWrite={canWrite}
      readOnlyReason={readOnlyReason}
    />
  )
}
