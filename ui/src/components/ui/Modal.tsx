/**
 * **功能**
 * 弹窗：标题、说明、正文、页脚动作。
 *
 * **归属**
 * `components` 通用件：只包 Radix Dialog。
 *
 * **口径**
 * **打开时焦点必须留在对话框内**。
 * 关闭动作走 `onOpenChange`，调用方持有开合状态。
 */

import type { ReactNode } from 'react'
import { Dialog } from 'radix-ui'
import { Icon } from '@/components/ui/Icon'

type ModalProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
}

/**
 * Adminator 只有静态 `.modal-demo` 标记，没有可用弹窗。
 * Radix Dialog 提供焦点陷阱、Esc 与遮罩语义。外观取 vendor 类。
 * 去掉 Radix 后键盘用户会被困在背景里。
 */
export function Modal({ open, onOpenChange, title, description, children, footer }: ModalProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="app-modal-backdrop" />
        <Dialog.Content
          className="modal-demo app-modal"
          aria-describedby={description ? 'app-modal-description' : undefined}
        >
          <div className="modal-head">
            <Dialog.Title className="modal-title">{title}</Dialog.Title>
            <Dialog.Close asChild>
              {/* Adminator 的弹窗示例用 `.mail-tool` 作关闭按钮。 */}
              <button type="button" className="mail-tool" aria-label="关闭弹窗">
                <Icon name="close" />
              </button>
            </Dialog.Close>
          </div>
          <div className="modal-body">
            {description ? (
              <p id="app-modal-description" className="app-muted">
                {description}
              </p>
            ) : null}
            {children}
          </div>
          {footer ? <div className="modal-foot">{footer}</div> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
