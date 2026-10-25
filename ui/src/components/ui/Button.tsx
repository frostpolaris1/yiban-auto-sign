/**
 * **功能**
 * 按钮：11 种变体、3 种尺寸。
 *
 * **归属**
 * `components` 通用件：只拼类名，不封装业务动作。
 *
 * **口径**
 * 默认 `type="button"`。表单内提交必须显式传 `type="submit"`。
 * 变体与类名一一对应，见 `VARIANT_CLASS`。
 */

import type { ComponentPropsWithoutRef } from 'react'
import { cx } from '@/lib/cx'

export type ButtonVariant =
  | 'primary'
  | 'secondary'
  | 'success'
  | 'warning'
  | 'danger'
  | 'info'
  | 'ghost'
  | 'soft-primary'
  | 'soft-danger'
  | 'outline-primary'
  | 'outline-danger'

export type ButtonSize = 'sm' | 'lg' | 'icon'

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: 'btn--primary',
  secondary: 'btn--secondary',
  success: 'btn--success',
  warning: 'btn--warning',
  danger: 'btn--danger',
  info: 'btn--info',
  ghost: 'btn--ghost',
  'soft-primary': 'btn--soft-primary',
  'soft-danger': 'btn--soft-danger',
  'outline-primary': 'btn--outline-primary',
  'outline-danger': 'btn--outline-danger',
}

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: 'btn--sm',
  lg: 'btn--lg',
  icon: 'btn--icon',
}

export type ButtonProps = ComponentPropsWithoutRef<'button'> & {
  variant?: ButtonVariant
  size?: ButtonSize
}

export function Button({ variant = 'ghost', size, className, type = 'button', ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      className={cx('btn', VARIANT_CLASS[variant], size && SIZE_CLASS[size], className)}
      {...rest}
    />
  )
}
