/**
 * **功能**
 * 右侧抽屉：标题、说明、正文、页脚动作。
 *
 * **归属**
 * `components` 通用件：只包 Radix Dialog 的侧滑变体。
 *
 * **口径**
 * 打开时焦点进入抽屉，由 Radix 焦点陷阱保证。
 * 关闭时把焦点还给 `returnFocusTo`。Radix 默认只认 `Dialog.Trigger`，
 * 本组件没有触发器，所以必须自己还原。
 */

import type { ReactNode } from 'react'
import { Dialog } from 'radix-ui'
import { Icon } from '@/components/ui/Icon'

type DrawerProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
  /** 打开抽屉的控件。关闭时焦点回到它。 */
  returnFocusTo?: HTMLElement | null
}

export function Drawer({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  returnFocusTo,
}: DrawerProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="app-drawer-backdrop" />
        <Dialog.Content
          className="app-drawer"
          aria-describedby={description ? 'app-drawer-description' : undefined}
          onCloseAutoFocus={(event) => {
            // 还原点随外部刷新被摘除时不还原（与 Modal 的 guard 同款），
            // 焦点交给 Radix 默认行为，不落到已消失的元素上。
            if (!returnFocusTo || !returnFocusTo.isConnected) return
            event.preventDefault()
            returnFocusTo.focus()
          }}
        >
          <div className="app-drawer-head">
            <Dialog.Title className="modal-title">{title}</Dialog.Title>
            <Dialog.Close asChild>
              <button type="button" className="mail-tool" aria-label="关闭详情">
                <Icon name="close" />
              </button>
            </Dialog.Close>
          </div>
          <div className="app-drawer-body">
            {description ? (
              <p id="app-drawer-description" className="app-muted">
                {description}
              </p>
            ) : null}
            {children}
          </div>
          {footer ? <div className="app-drawer-foot">{footer}</div> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
