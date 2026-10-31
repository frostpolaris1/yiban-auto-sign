/**
 * **功能**
 * 写操作按钮：在途时禁用并标记 `aria-busy`。
 *
 * **归属**
 * `components` 通用件：包一层 `Button`，不改它的变体、尺寸与语义。
 *
 * **口径**
 * 在途即 `disabled`：浏览器在输入层面吞掉重复点击，防重复提交。
 * 在途同时渲染小加载环；`busyLabel` 缺省时保留原文案。
 * 落定后回到 `Button` 的普通行为，本组件不留任何状态。
 */

import type { ComponentPropsWithoutRef } from 'react'
import { Button, type ButtonSize, type ButtonVariant } from '@/components/ui/Button'

export type BusyButtonProps = ComponentPropsWithoutRef<'button'> & {
  variant?: ButtonVariant
  size?: ButtonSize
  /** 写操作在途。在途期间按钮禁用并标记 `aria-busy`。 */
  busy?: boolean
  /** 在途文案。缺省保留原 children。 */
  busyLabel?: string
}

export function BusyButton({
  busy = false,
  busyLabel,
  disabled,
  children,
  variant,
  size,
  className,
  type = 'button',
  ...rest
}: BusyButtonProps) {
  return (
    <Button
      type={type}
      variant={variant}
      size={size}
      className={className}
      disabled={disabled || busy}
      aria-busy={busy ? true : undefined}
      {...rest}
    >
      {busy ? (
        <>
          <span className="spinner sm" aria-hidden="true" />
          {busyLabel ?? children}
        </>
      ) : (
        children
      )}
    </Button>
  )
}
