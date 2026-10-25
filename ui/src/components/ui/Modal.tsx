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
 * Adminator has no working modal: only static `.modal-demo` markup.
 * Radix Dialog supplies focus trap, Escape, and backdrop semantics.
 * Adminator supplies the visual surface.
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
              {/* Adminator's modal demo uses `.mail-tool` for the close button. */}
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
