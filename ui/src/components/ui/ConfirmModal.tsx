/**
 * **功能**
 * 确认弹窗：一句话说明后果、一个取消、一个确认。
 *
 * **归属**
 * `components` 通用件：只排版，动作由调用方给。
 *
 * **口径**
 * 确认前调用方不得发请求：本组件只在确认按钮点击时回调。
 * 失败文案显示在弹窗内，弹窗不自动关闭。
 * `layer` 透传给 `Modal`：叠在另一个弹窗之上时给更大的层号。
 */

import type { ReactNode } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'

type ConfirmModalProps = {
  open: boolean
  title: string
  description?: ReactNode
  confirmText: string
  pending?: boolean
  error?: string | null
  tone?: 'primary' | 'danger'
  returnFocusTo?: HTMLElement | null
  /** 叠层序号，透传给 `Modal`；缺省 1。 */
  layer?: number
  onConfirm: () => void
  onClose: () => void
  children?: ReactNode
}

export function ConfirmModal({
  open,
  title,
  description,
  confirmText,
  pending,
  error,
  tone = 'primary',
  returnFocusTo,
  layer,
  onConfirm,
  onClose,
  children,
}: ConfirmModalProps) {
  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title={title}
      description={description}
      returnFocusTo={returnFocusTo}
      layer={layer}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button variant={tone} onClick={onConfirm} disabled={pending}>
            {confirmText}
          </Button>
        </>
      }
    >
      {children}
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
