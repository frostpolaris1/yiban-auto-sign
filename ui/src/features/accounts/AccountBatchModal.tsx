/**
 * **功能**
 * 账号批量动作的确认弹窗：影响范围、二次口令、逐项结果。
 *
 * **归属**
 * `features/accounts` 展示层：请求由 `AccountsView` 发。
 *
 * **口径**
 * 确认前零请求；驳回必须填理由（1..200 字，与契约一致）。
 * 二次口令是操作者本人本站登录口令，关闭即清空。
 * 部分失败时弹窗不关：结果清单按请求顺序给 ok/error，
 * 失败项带服务端稳定 code 与用户可读 message。
 */

import { useEffect, useState } from 'react'
import type { Account, AccountBatchAction, AccountBatchResult } from '@/lib/api/account'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { PasswordInput, TextAreaInput } from '@/components/ui/Input'

export const ACCOUNT_BATCH_LABELS: Record<AccountBatchAction, string> = {
  review_approve: '审核通过',
  review_reject: '驳回',
  pause: '暂停',
  resume: '恢复签到',
  delete: '删除',
  restore: '恢复账号',
}

const DESCRIPTIONS: Record<AccountBatchAction, string> = {
  review_approve: '通过后账号参与签到；每项独立事务，互不阻断。',
  review_reject: '驳回必须填写理由；每项独立事务，互不阻断。',
  pause: '暂停后账号停止自动签到，直到恢复。',
  resume: '恢复后账号重新参与自动签到。',
  delete: '删除后账号进入软删除状态，凭据级联隐藏。',
  restore: '恢复后账号保持删除前状态；同属主已有活跃账号的项会失败。',
}

type AccountBatchModalProps = {
  open: boolean
  action: AccountBatchAction | null
  rows: Account[]
  pending: boolean
  error: string | null
  result: AccountBatchResult | null
  onClose: () => void
  onConfirm: (password: string, reason?: string) => void
}

export function AccountBatchModal({
  open,
  action,
  rows,
  pending,
  error,
  result,
  onClose,
  onConfirm,
}: AccountBatchModalProps) {
  const [password, setPassword] = useState('')
  const [reason, setReason] = useState('')

  // 关闭或换动作都清空口令与理由：秘密不跨次复用。
  useEffect(() => {
    if (!open) {
      setPassword('')
      setReason('')
    }
  }, [open])
  useEffect(() => {
    setPassword('')
    setReason('')
  }, [action])

  const needsReason = action === 'review_reject'
  const reasonMissing = needsReason && reason.trim() === ''
  const labels = new Map(rows.map((row) => [row.id, row.phone_masked]))
  const label = action ? ACCOUNT_BATCH_LABELS[action] : '批量操作'

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title={`批量${label}？`}
      description={
        action
          ? `将作用于已选 ${rows.length} 个账号。${DESCRIPTIONS[action]}`
          : undefined
      }
      footer={
        result ? (
          <Button variant="ghost" onClick={onClose}>
            关闭
          </Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose} disabled={pending}>
              取消
            </Button>
            <Button
              variant={action === 'delete' ? 'danger' : 'primary'}
              disabled={pending || password.trim() === '' || reasonMissing}
              onClick={() => onConfirm(password, needsReason ? reason.trim() : undefined)}
            >
              {pending ? '执行中…' : `确认${label}`}
            </Button>
          </>
        )
      }
    >
      <ul className="app-batch-impact" aria-label="影响范围">
        {rows.map((row) => (
          <li key={row.id} className="data-cell-mono">
            {row.phone_masked} · {row.display_name}
          </li>
        ))}
      </ul>

      {result ? (
        <div className="app-batch-result" role="status" aria-label="批量结果">
          <strong>
            结果：{result.items.filter((item) => item.status === 'ok').length} 条成功，
            {result.items.filter((item) => item.status === 'error').length} 条失败
          </strong>
          {result.items.map((item) => (
            <div key={item.id} className="app-batch-result-row">
              <span className="data-cell-mono">{labels.get(item.id) ?? item.id}</span>
              {item.status === 'ok' ? (
                <span>成功</span>
              ) : (
                <span>
                  {item.code ?? 'error'} · {item.message ?? '未提供原因'}
                </span>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="app-form-stack">
          {needsReason ? (
            <TextAreaInput
              id="account-batch-reason"
              label="驳回理由"
              hint="驳回必须填写理由，最多 200 字。"
              maxLength={200}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          ) : null}
          <PasswordInput
            id="account-batch-password"
            label="你的登录口令"
            hint="批量动作需要二次确认；口令不会保存。"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
      )}

      {error ? (
        <div className="app-modal-error">
          <Alert tone="danger" title="请求失败">
            {error}
          </Alert>
        </div>
      ) : null}
    </Modal>
  )
}
