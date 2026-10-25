import { useState, type ReactElement, type ReactNode } from 'react'
import { DropdownMenu } from 'radix-ui'
import { cx } from '@/lib/cx'

type MenuProps = {
  trigger: ReactElement
  children: ReactNode
  align?: 'start' | 'end'
  profile?: boolean
}

/**
 * Radix DropdownMenu rendered inside an Adminator `.dd-wrap`.
 *
 * React tracks open state so `.dd-wrap.is-open` drives the Adminator
 * opacity/visibility transition. Radix supplies keyboard navigation,
 * typeahead, and outside-click dismissal.
 */
export function Menu({ trigger, children, align = 'end', profile }: MenuProps) {
  const [open, setOpen] = useState(false)

  return (
    <div className={cx('dd-wrap', open && 'is-open')}>
      <DropdownMenu.Root open={open} onOpenChange={setOpen} modal={false}>
        <DropdownMenu.Trigger asChild>{trigger}</DropdownMenu.Trigger>
        <DropdownMenu.Content
          className={cx('dd-menu', profile && 'dd-profile')}
          align={align}
          sideOffset={10}
        >
          {children}
        </DropdownMenu.Content>
      </DropdownMenu.Root>
    </div>
  )
}

type MenuItemProps = {
  children: ReactNode
  danger?: boolean
  onSelect?: () => void
}

export function MenuItem({ children, danger, onSelect }: MenuItemProps) {
  return (
    <DropdownMenu.Item
      className={cx('dd-menu-item', danger && 'danger')}
      onSelect={() => onSelect?.()}
    >
      {children}
    </DropdownMenu.Item>
  )
}

export function MenuDivider() {
  return <DropdownMenu.Separator className="dd-divider" />
}
