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
 * Radix Tabs with Adminator `.tabs` / `.tab` / `.tab-panel` classes.
 * Radix owns roving focus and arrow keys; React tracks the value so the
 * Adminator `is-active` classes stay exact.
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
