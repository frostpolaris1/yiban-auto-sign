/**
 * **功能**
 * 写操作确认模态：暂停／恢复／签到／验证／评审。
 *
 * **归属**
 * `features/accounts` 展示层：只收集确认与评审表单，发请求由调用方做。
 *
 * **口径**
 * 任何写操作都必须先过这个模态。确认前不发请求。
 * 驳回必须填理由，未填时确认按钮禁用。理由上限 200 字，与契约一致。
 * 验证结论留在模态内展示；其余操作成功后关闭。
 */

import { useEffect, useState } from 'react'
import type { Account, AccountVerifyResult } from '@/lib/api/account'
import { Alert } from '@/components/ui/Alert'
import { Button, type ButtonVariant } from '@/components/ui/Button'
import { SelectInput, TextAreaInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { statusLabel } from './display'
import type { AccountActionKind } from './permissions'

export type ReviewInput = {
  decision: 'approve' | 'reject'
  reason?: string
}

export type AccountActionState = {
  account: Account
  kind: AccountActionKind
}

const TITLES: Record<AccountActionKind, string> = {
  pause: '暂停账号？',
  resume: '恢复账号？',
  sign: '立即签到？',
  verify: '验证凭据？',
  review: '评审账号？',
}

const CONFIRMS: Record<AccountActionKind, string> = {
  pause: '确认暂停',
  resume: '确认恢复',
  sign: '确认签到',
  verify: '确认验证',
  review: '提交评审',
}

const CONFIRM_VARIANTS: Record<AccountActionKind, ButtonVariant> = {
  pause: 'warning',
  resume: 'success',
  sign: 'primary',
  verify: 'primary',
  review: 'primary',
}

const EXPLANATIONS: Record<AccountActionKind, string> = {
  pause: '暂停后该账号停止自动签到，直到恢复。',
  resume: '恢复后该账号重新参与自动签到。',
  sign: '立即为今天创建一个签到任务。窗口关闭、正在运行或今日已出结论时，服务端返回冲突。',
  verify: '用已存凭据向平台验证一次。结论留在本窗口展示。',
  review: '通过后账号参与签到；驳回必须填写理由。',
}

type AccountActionModalProps = {
  state: AccountActionState | null
  pending: boolean
  error: string | null
  verifyResult: AccountVerifyResult | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onConfirm: (review?: ReviewInput) => void
}

export function AccountActionModal({
  state,
  pending,
  error,
  verifyResult,
  returnFocusTo,
  onClose,
  onConfirm,
}: AccountActionModalProps) {
  const [decision, setDecision] = useState<'approve' | 'reject'>('approve')
  const [reason, setReason] = useState('')

  useEffect(() => {
    setDecision('approve')
    setReason('')
  }, [state?.account.id, state?.kind])

  const kind = state?.kind
  const reviewNeedsReason = kind === 'review' && decision === 'reject' && !reason.trim()

  return (
    <Modal
      open={state !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={kind ? TITLES[kind] : '账号操作'}
      description={
        state ? `账号 ${state.account.phone_masked} · 当前状态 ${statusLabel(state.account.status)}` : undefined
      }
      returnFocusTo={returnFocusTo}
      footer={
        verifyResult ? (
          <Button variant="ghost" onClick={onClose}>
            关闭
          </Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose} disabled={pending}>
              取消
            </Button>
            <Button
              variant={kind ? CONFIRM_VARIANTS[kind] : 'primary'}
              onClick={() =>
                onConfirm(
                  kind === 'review'
                    ? { decision, reason: decision === 'reject' ? reason.trim() : undefined }
                    : undefined,
                )
              }
              disabled={pending || reviewNeedsReason}
            >
              {kind ? CONFIRMS[kind] : '确认'}
            </Button>
          </>
        )
      }
    >
      {kind ? <p className="app-muted">{EXPLANATIONS[kind]}</p> : null}

      {kind === 'review' ? (
        <div className="app-form-stack">
          <SelectInput
            id="account-review-decision"
            label="评审结论"
            options={[
              { value: 'approve', label: '通过' },
              { value: 'reject', label: '驳回' },
            ]}
            value={decision}
            onChange={(event) => setDecision(event.target.value === 'reject' ? 'reject' : 'approve')}
          />
          <TextAreaInput
            id="account-review-reason"
            label="驳回理由"
            hint="驳回必须填写理由，最多 200 字。"
            maxLength={200}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            disabled={decision !== 'reject'}
          />
        </div>
      ) : null}

      {error ? (
        <div className="app-modal-error">
          <Alert tone="danger" title="请求失败">
            {error}
          </Alert>
        </div>
      ) : null}

      {verifyResult ? (
        <div className="app-modal-error">
          <Alert
            tone={verifyResult.ok ? 'success' : 'warning'}
            title={verifyResult.ok ? '验证通过' : `验证失败：${verifyResult.code}`}
          >
            {verifyResult.message ?? (verifyResult.ok ? '平台接受当前凭据。' : '平台未接受当前凭据。')}
          </Alert>
        </div>
      ) : null}
    </Modal>
  )
}
