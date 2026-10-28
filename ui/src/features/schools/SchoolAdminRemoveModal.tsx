/**
 * **功能**
 * 移除学校管理员的危险确认模态。
 *
 * **归属**
 * `features/schools` 展示层：发请求由调用方做。
 *
 * **口径**
 * 确认前零请求。204 后列表刷新、本模态关闭、焦点回到移除按钮。
 * 危险样式只用 danger 令牌。
 */

import type { SchoolAdmin } from '@/lib/api/school'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'

type SchoolAdminRemoveModalProps = {
  admin: SchoolAdmin | null
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onConfirm: () => void
}

export function SchoolAdminRemoveModal({
  admin,
  pending,
  error,
  returnFocusTo,
  onClose,
  onConfirm,
}: SchoolAdminRemoveModalProps) {
  return (
    <Modal
      open={admin !== null}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title="移除管理员？"
      description={admin ? `管理员 ${admin.email}` : undefined}
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button variant="danger" disabled={pending} onClick={onConfirm}>
            确认移除
          </Button>
        </>
      }
    >
      <Alert tone="danger" title="解除绑定">
        移除后该用户不再管理此学校。确认后本行从管理员列表移除。
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
