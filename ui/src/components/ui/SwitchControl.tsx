/**
 * **功能**
 * 开关控件：标签、初值、禁用态。
 *
 * **归属**
 * `components` 通用件：只包 Radix Switch。
 *
 * **口径**
 * 状态自持，调用方不传受控值。
 * 选中样式在 app.css 补写：vendor 只认原生 input。
 */

import { useState } from 'react'
import { Switch as RadixSwitch } from 'radix-ui'

type SwitchControlProps = {
  label: string
  defaultChecked?: boolean
  name?: string
  disabled?: boolean
}

/**
 * Radix Switch，外观取 Adminator `.switch`。
 * vendor 的目标是原生 input，勾选态因此需在 app.css 重写。
 * 少这条规则，开关在任何状态下都显示为未选中。
 */
export function SwitchControl({ label, defaultChecked = false, name, disabled }: SwitchControlProps) {
  const [checked, setChecked] = useState(defaultChecked)

  return (
    <RadixSwitch.Root
      className="switch app-switch"
      checked={checked}
      onCheckedChange={setChecked}
      aria-label={label}
      name={name}
      disabled={disabled}
    >
      <RadixSwitch.Thumb className="track" />
      <span>{label}</span>
    </RadixSwitch.Root>
  )
}
