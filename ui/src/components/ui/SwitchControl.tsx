import { useState } from 'react'
import { Switch as RadixSwitch } from 'radix-ui'

type SwitchControlProps = {
  label: string
  defaultChecked?: boolean
  name?: string
  disabled?: boolean
}

/**
 * Radix Switch with the Adminator `.switch` surface.
 * The checked styles live in app.css because Adminator targets a native input.
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
