/**
 * **功能**
 * 开关控件：支持非受控（组件画廊）与受控（设置矩阵）两种模式。
 *
 * **归属**
 * `components` 通用件：只包 Radix Switch。
 *
 * **口径**
 * 未传 `checked` 时状态自持；传入 `checked` 时转为受控，供异步保存场景使用。
 * 选中样式在 app.css 补写：vendor 只认原生 input。
 */

import { useState } from 'react'
import { Switch as RadixSwitch } from 'radix-ui'
import { cx } from '@/lib/cx'

type SwitchControlProps = {
  label: string
  defaultChecked?: boolean
  checked?: boolean
  onCheckedChange?: (checked: boolean) => void
  hideLabel?: boolean
  name?: string
  disabled?: boolean
  'aria-disabled'?: boolean
  'aria-busy'?: boolean
  'aria-describedby'?: string
  title?: string
  className?: string
}

/**
 * Radix Switch，外观取 Adminator `.switch`。
 * vendor 的目标是原生 input，勾选态因此需在 app.css 重写。
 * 少这条规则，开关在任何状态下都显示为未选中。
 */
export function SwitchControl({
  label,
  defaultChecked = false,
  checked,
  onCheckedChange,
  hideLabel = false,
  name,
  disabled,
  'aria-disabled': ariaDisabled,
  'aria-busy': ariaBusy,
  'aria-describedby': ariaDescribedBy,
  title,
  className,
}: SwitchControlProps) {
  const [internalChecked, setInternalChecked] = useState(defaultChecked)
  const isControlled = checked !== undefined
  const value = isControlled ? checked : internalChecked

  return (
    <RadixSwitch.Root
      className={cx('switch app-switch', className)}
      checked={value}
      onCheckedChange={(next) => {
        if (disabled || ariaDisabled) return
        if (!isControlled) setInternalChecked(next)
        onCheckedChange?.(next)
      }}
      aria-label={label}
      aria-disabled={ariaDisabled || undefined}
      aria-busy={ariaBusy || undefined}
      aria-describedby={ariaDescribedBy}
      title={title}
      name={name}
      disabled={disabled}
    >
      <RadixSwitch.Thumb className="track" />
      {hideLabel ? null : <span>{label}</span>}
    </RadixSwitch.Root>
  )
}
