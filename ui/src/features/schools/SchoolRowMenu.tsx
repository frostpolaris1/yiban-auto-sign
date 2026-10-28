/**
 * **功能**
 * 行内「更多」菜单：编辑与管理员两项的收纳入口。
 *
 * **归属**
 * `features/schools` 展示层：权限判断走 permissions，动作回调由调用方给。
 *
 * **口径**
 * 置灰项保留在菜单里并带原因；触发器元素是模态关闭后的焦点还原点。
 * 合同没有删除端点，菜单不出删除项。
 * 触发器点击不得冒泡到行，否则会连带打开抽屉。
 */

import { useRef } from 'react'
import type { Role } from '@/lib/api/auth'
import type { School } from '@/lib/api/school'
import { Icon, type IconName } from '@/components/ui/Icon'
import { Menu, MenuItem } from '@/components/ui/Menu'
import { denyReason, type SchoolAction } from './permissions'

type SchoolRowMenuProps = {
  school: School
  role: Role | null
  onEdit: (school: School, opener: HTMLElement | null) => void
  onAdmins: (school: School, opener: HTMLElement | null) => void
}

type RowMenuEntry = {
  item: Extract<SchoolAction, 'edit' | 'admins'>
  label: string
  icon: IconName
  onRun: () => void
}

export function SchoolRowMenu({ school, role, onEdit, onAdmins }: SchoolRowMenuProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const entries: RowMenuEntry[] = [
    {
      item: 'edit',
      label: '编辑',
      icon: 'edit',
      onRun: () => onEdit(school, triggerRef.current),
    },
    {
      item: 'admins',
      label: '管理员',
      icon: 'userCog',
      onRun: () => onAdmins(school, triggerRef.current),
    },
  ]

  return (
    <Menu
      trigger={
        <button
          type="button"
          ref={triggerRef}
          className="btn btn--ghost btn--icon"
          aria-label={`更多操作 ${school.name}`}
          onClick={(event) => event.stopPropagation()}
        >
          <Icon name="menu" />
        </button>
      }
    >
      {entries.map(({ item, label, icon, onRun }) => {
        const reason = denyReason(role, item)
        return (
          <MenuItem key={item} disabled={reason !== null} onSelect={onRun}>
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
