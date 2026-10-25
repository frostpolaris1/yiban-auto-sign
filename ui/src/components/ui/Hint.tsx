import type { ReactElement } from 'react'
import { Tooltip } from 'radix-ui'

type HintProps = {
  label: string
  children: ReactElement
}

/**
 * Radix Tooltip. Content stays inline (no portal) so Adminator's
 * `.popover-demo .pop` rule supplies the visual style.
 */
export function Hint({ label, children }: HintProps) {
  return (
    <Tooltip.Provider delayDuration={150}>
      <span className="popover-demo">
        <Tooltip.Root>
          <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
          <Tooltip.Content className="pop" sideOffset={8}>
            {label}
          </Tooltip.Content>
        </Tooltip.Root>
      </span>
    </Tooltip.Provider>
  )
}
