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
 * 禁用项置灰不隐藏：原因由调用方放在条目内，两套主题下可读。
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
        {/* 不包 Portal 时菜单渲染在触发点子树里：点击菜单项会沿 DOM
            冒泡到外层可点容器（如表格行的详情触发）。Portal 断开该路径。 */}
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            className={cx('dd-menu', profile && 'dd-profile')}
            align={align}
            sideOffset={10}
          >
            {children}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
  )
}

type MenuItemProps = {
  children: ReactNode
  danger?: boolean
  disabled?: boolean
  onSelect?: () => void
}

export function MenuItem({ children, danger, disabled, onSelect }: MenuItemProps) {
  return (
    <DropdownMenu.Item
      className={cx('dd-menu-item', danger && 'danger')}
      disabled={disabled}
      onClick={(event) => {
        // 菜单内容虽经 Portal 挂到 body，但 React 合成事件沿 fiber 父链
        // （MenuItem → Menu → 调用方）冒泡，不沿 DOM 树——Portal 外的
        // 可点祖先（如表格行的详情触发）会被误触发。在源头断开。
        event.stopPropagation()
      }}
      onSelect={(event) => {
        event.stopPropagation()
        if (!disabled) onSelect?.()
      }}
    >
      {children}
    </DropdownMenu.Item>
  )
}

export function MenuDivider() {
  return <DropdownMenu.Separator className="dd-divider" />
}
