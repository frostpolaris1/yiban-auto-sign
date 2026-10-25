/**
 * **功能**
 * 标签页：标签列表与对应面板。
 *
 * **归属**
 * `components` 通用件：只包 Radix Tabs。
 *
 * **口径**
 * 初值缺省取第一项。空列表时值为空串。
 * `is-active` 由 React 按当前值给，不用 Radix 的 data 属性。
 */

import { useState, type ReactNode } from 'react'
import { Tabs as RadixTabs } from 'radix-ui'
import { cx } from '@/lib/cx'

export type TabItem = {
  value: string
  label: ReactNode
  content: ReactNode
}

type TabsProps = {
  items: TabItem[]
  defaultValue?: string
  label: string
  pills?: boolean
}

/**
 * Radix Tabs，类名取 Adminator `.tabs`、`.tab`、`.tab-panel`。
 * Radix 管焦点漫游与方向键。React 记当前值，保证 `is-active` 准确。
 * 两边各记一份状态时，键盘切换会与高亮不同步。
 */
export function Tabs({ items, defaultValue, label, pills }: TabsProps) {
  const [value, setValue] = useState(defaultValue ?? items[0]?.value ?? '')

  return (
    <RadixTabs.Root value={value} onValueChange={setValue}>
      <RadixTabs.List className={pills ? 'tabs pills' : 'tabs'} aria-label={label}>
        {items.map((item) => (
          <RadixTabs.Trigger
            key={item.value}
            value={item.value}
            className={cx('tab', value === item.value && 'is-active')}
          >
            {item.label}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {items.map((item) => (
        <RadixTabs.Content
          key={item.value}
          value={item.value}
          className={cx('tab-panel', value === item.value && 'is-active')}
        >
          {item.content}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  )
}
