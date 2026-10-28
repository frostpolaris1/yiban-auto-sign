/**
 * **功能**
 * 删除确认模态：软删除的危险确认。
 *
 * **归属**
 * `features/accounts` 展示层：发请求由调用方做。
 *
 * **口径**
 * 危险样式只用 danger 令牌。文案写明软删与凭据级联隐藏。
 * 确认前零请求；204 后行消失、关联抽屉关闭、焦点回到发起项。
 */

import type { Account } from '@/lib/api/account'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { statusLabel } from './display'

type AccountDeleteModalProps = {
  account: Account | null
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onConfirm: () => void
}

export function AccountDeleteModal({
  account,
  pending,
  error,
  returnFocusTo,
  onClose,
  onConfirm,
}: AccountDeleteModalProps) {
  return (
    <Modal
      open={account !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title="删除账号？"
      description={
        account
          ? `账号 ${account.phone_masked} · 当前状态 ${statusLabel(account.status)}`
          : undefined
      }
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button variant="danger" disabled={pending} onClick={onConfirm}>
            确认删除
          </Button>
        </>
      }
    >
      <Alert tone="danger" title="软删除">
        删除后账号进入软删除状态，凭据将被级联隐藏。确认后本行从列表移除。
      </Alert>
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
