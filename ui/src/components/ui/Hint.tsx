/**
 * **功能**
 * 悬停提示。
 *
 * **归属**
 * `components` 通用件：只包一层 Radix Tooltip。
 *
 * **口径**
 * 触发元素由调用方给。组件不自造按钮。
 * 延迟 150 毫秒。
 */

import type { ReactElement } from 'react'
import { Tooltip } from 'radix-ui'

type HintProps = {
  label: string
  children: ReactElement
}

/**
 * Radix Tooltip，外观取 Adminator `.popover-demo .pop`。
 * 内容**不进门户**，留在原 DOM。挂到门户后 vendor 选择器不生效。
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
