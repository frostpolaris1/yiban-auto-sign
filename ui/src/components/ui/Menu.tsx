/**
 * **功能**
 * 下拉菜单：触发器、菜单项、分隔线。
 *
 * **归属**
 * `components` 通用件：只包 Radix DropdownMenu。
 *
 * **口径**
 * 开合状态由 React 持有，`.dd-wrap.is-open` 驱动 vendor 过渡。
 * 键盘导航、首字母跳转、点外部关闭交给 Radix。
 */

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
 * Radix DropdownMenu，外观取 Adminator `.dd-wrap`。
 * 不设 `is-open` 时 vendor 的透明度过渡不触发，菜单会直接闪现。
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
