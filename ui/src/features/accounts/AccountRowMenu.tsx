/**
 * **功能**
 * 行内「更多」菜单：活行收桌面端次要行操作（与分隔线）及编辑、位置、删除；
 * 已删行收恢复与彻底删除。
 *
 * **归属**
 * `features/accounts` 展示层：权限判断走 permissions，动作回调由调用方给。
 *
 * **口径**
 * 置灰项保留在菜单里并带原因；触发器元素是模态关闭后的焦点还原点。
 * 触发器点击不得冒泡到行，否则会连带打开抽屉。
 * 640px 以下行卡模式不传 `secondaryActions`，菜单保持编辑/位置/删除三项原状。
 */

import { useRef } from 'react'
import type { Account } from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import { Icon, type IconName } from '@/components/ui/Icon'
import { Menu, MenuDivider, MenuItem } from '@/components/ui/Menu'
import { editActionLabel } from './display'
import { menuDenyReason, type AccountActionKind, type RowMenuItem } from './permissions'

export type SecondaryRowAction = {
  kind: AccountActionKind
  label: string
  icon: IconName
  reason: string | null
}

type AccountRowMenuProps = {
  account: Account
  role: Role | null
  /** 桌面端收入菜单的次要行动作；移动端行卡不传，避免与行内按钮重复。 */
  secondaryActions?: readonly SecondaryRowAction[]
  onAction?: (account: Account, kind: AccountActionKind, opener: HTMLElement) => void
  onEdit: (account: Account, opener: HTMLElement | null) => void
  onLocation: (account: Account, opener: HTMLElement | null) => void
  onDelete: (account: Account, opener: HTMLElement | null) => void
  onRestore: (account: Account, opener: HTMLElement | null) => void
  onPurge: (account: Account, opener: HTMLElement | null) => void
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
  secondaryActions,
  onAction,
  onEdit,
  onLocation,
  onDelete,
  onRestore,
  onPurge,
}: AccountRowMenuProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  // 软删行只有恢复与彻底删除两条路：编辑、位置与删除对已删行都是 404，不放进菜单。
  const entries: RowMenuEntry[] =
    account.deleted_at != null
      ? [
          {
            item: 'restore',
            label: '恢复',
            icon: 'rotateCcw',
            onRun: () => onRestore(account, triggerRef.current),
          },
          {
            item: 'purge',
            label: '彻底删除',
            icon: 'trash',
            danger: true,
            onRun: () => onPurge(account, triggerRef.current),
          },
        ]
      : [
          {
            item: 'edit',
            label: editActionLabel(account.status),
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

  const hasSecondary =
    account.deleted_at == null &&
    Boolean(onAction) &&
    Boolean(secondaryActions && secondaryActions.length > 0)

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
      {hasSecondary
        ? secondaryActions!.map(({ kind, label, icon, reason }) => (
            <MenuItem
              key={kind}
              disabled={reason !== null}
              onSelect={() => {
                if (triggerRef.current && onAction) {
                  onAction(account, kind, triggerRef.current)
                }
              }}
            >
              <Icon name={icon} />
              <span className="dd-item-stack">
                <span>{label}</span>
                {reason ? <span className="dd-item-reason">{reason}</span> : null}
              </span>
            </MenuItem>
          ))
        : null}
      {hasSecondary ? <MenuDivider /> : null}
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
