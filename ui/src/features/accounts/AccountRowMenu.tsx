/**
 * **功能**
 * 行内「更多」菜单：编辑、位置、删除三项的收纳入口。
 *
 * **归属**
 * `features/accounts` 展示层：权限判断走 permissions，动作回调由调用方给。
 *
 * **口径**
 * 置灰项保留在菜单里并带原因；触发器元素是模态关闭后的焦点还原点。
 * 触发器点击不得冒泡到行，否则会连带打开抽屉。
 */

import { useRef } from 'react'
import type { Account } from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import { Icon, type IconName } from '@/components/ui/Icon'
import { Menu, MenuItem } from '@/components/ui/Menu'
import { menuDenyReason, type RowMenuItem } from './permissions'

type AccountRowMenuProps = {
  account: Account
  role: Role | null
  onEdit: (account: Account, opener: HTMLElement | null) => void
  onLocation: (account: Account, opener: HTMLElement | null) => void
  onDelete: (account: Account, opener: HTMLElement | null) => void
}

type RowMenuEntry = {
  item: RowMenuItem
  label: string
  icon: IconName
  danger?: boolean
  onRun: () => void
}

export function AccountRowMenu({
  account,
  role,
  onEdit,
  onLocation,
  onDelete,
}: AccountRowMenuProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const entries: RowMenuEntry[] = [
    {
      item: 'edit',
      label: '编辑',
      icon: 'edit',
      onRun: () => onEdit(account, triggerRef.current),
    },
    {
      item: 'location',
      label: '位置',
      icon: 'mapPin',
      onRun: () => onLocation(account, triggerRef.current),
    },
    {
      item: 'delete',
      label: '删除',
      icon: 'trash',
      danger: true,
      onRun: () => onDelete(account, triggerRef.current),
    },
  ]

  return (
    <Menu
      trigger={
        <button
          type="button"
          ref={triggerRef}
          className="btn btn--ghost btn--icon"
          aria-label={`更多操作 ${account.phone_masked}`}
          onClick={(event) => event.stopPropagation()}
        >
          <Icon name="menu" />
        </button>
      }
    >
      {entries.map(({ item, label, icon, danger, onRun }) => {
        const reason = menuDenyReason(role, item)
        return (
          <MenuItem key={item} danger={danger} disabled={reason !== null} onSelect={onRun}>
            <Icon name={icon} />
            <span className="dd-item-stack">
              <span>{label}</span>
              {reason ? <span className="dd-item-reason">{reason}</span> : null}
            </span>
          </MenuItem>
        )
      })}
    </Menu>
  )
}
