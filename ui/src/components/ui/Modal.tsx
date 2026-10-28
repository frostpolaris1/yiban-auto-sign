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

import { useId, type ReactNode } from 'react'
import { Dialog } from 'radix-ui'
import { Icon } from '@/components/ui/Icon'
import { cx } from '@/lib/cx'

type ModalProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
  /**
   * 打开弹窗的控件。关闭时焦点回到它；还原点已被摘除（如删除行）
   * 时保持 Radix 默认行为，焦点归属由调用方在成功回调里指定。
   */
  returnFocusTo?: HTMLElement | null
  /**
   * 叠层序号，缺省 1。每高一档遮罩与内容各高 20：
   * 第 2 层压暗第 1 层，第 3 层压暗第 2 层。焦点陷阱由 Radix 的层栈管，只陷最上层。
   */
  layer?: number
  /**
   * 正文可能超出视口时置真：内容限高、正文滚动、页脚常驻。
   * 短弹窗不要开，保持既有布局逐像素不变。
   */
  scrollBody?: boolean
}

/**
 * Adminator 只有静态 `.modal-demo` 标记，没有可用弹窗。
 * Radix Dialog 提供焦点陷阱、Esc 与遮罩语义。外观取 vendor 类。
 * 去掉 Radix 后键盘用户会被困在背景里。
 * 无 `Dialog.Trigger` 时 Radix 关闭后把焦点落到 body，故由调用方给还原点。
 */
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  returnFocusTo,
  layer,
  scrollBody,
}: ModalProps) {
  // 叠层时两份弹窗同时在场。固定 id 会重复，故每个实例各取一个。
  const descriptionId = useId()
  // 弹窗层级：遮罩 190 起，内容 200 起；每层各加 20。
  const level = Math.max(1, Math.trunc(layer ?? 1))
  const contentZ = 200 + (level - 1) * 20
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay
          className="app-modal-backdrop"
          style={{ zIndex: contentZ - 10 }}
        />
        <Dialog.Content
          className={cx('modal-demo app-modal', scrollBody && 'app-modal-scroll')}
          style={{ zIndex: contentZ }}
          aria-describedby={description ? descriptionId : undefined}
          onCloseAutoFocus={(event) => {
            if (!returnFocusTo) return
            if (!returnFocusTo.isConnected) return
            event.preventDefault()
            returnFocusTo.focus()
          }}
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
              <p id={descriptionId} className="app-muted">
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
