/**
 * **功能**
 * 用户筛选栏：角色、状态、邮箱前缀。
 *
 * **归属**
 * `features/user` 展示层：只受控渲染，不改筛选状态。
 *
 * **口径**
 * 三个控件同属一行，布局复用账号页的 `.app-filters`。
 * 邮箱前缀是 `GET /api/users` 的 `q`：只匹配地址开头，搜索框不写手机号口径。
 * 回车由调用方立即生效；输入防抖也在调用方。
 */

import type { UserRole, UserStatus } from '@/lib/api/user'
import { Icon } from '@/components/ui/Icon'
import { USER_ROLE_OPTIONS, USER_STATUS_OPTIONS } from './display'

export type UserFilterValues = {
  role?: UserRole
  status?: UserStatus
  q?: string
}

type UserFiltersProps = {
  values: UserFilterValues
  qInput: string
  onRoleChange: (role?: UserRole) => void
  onStatusChange: (status?: UserStatus) => void
  onQInputChange: (value: string) => void
  onQCommit: () => void
}

export function UserFilters({
  values,
  qInput,
  onRoleChange,
  onStatusChange,
  onQInputChange,
  onQCommit,
}: UserFiltersProps) {
  return (
    <div className="app-filters">
      <div className="input-icon app-filter-q">
        <span className="ico">
          <Icon name="search" />
        </span>
        <input
          className="input"
          type="search"
          aria-label="邮箱前缀筛选"
          placeholder="邮箱前缀，回车立即查询"
          value={qInput}
          onChange={(event) => onQInputChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return
            event.preventDefault()
            onQCommit()
          }}
        />
      </div>

      <div className="app-filter">
        <span className="app-filter-label">角色</span>
        <select
          className="select"
          aria-label="角色筛选"
          value={values.role ?? ''}
          onChange={(event) => {
            const value = event.target.value
            onRoleChange(value === '' ? undefined : (value as UserRole))
          }}
        >
          <option value="">全部角色</option>
          {USER_ROLE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="app-filter">
        <span className="app-filter-label">状态</span>
        <select
          className="select"
          aria-label="状态筛选"
          value={values.status ?? ''}
          onChange={(event) => {
            const value = event.target.value
            onStatusChange(value === '' ? undefined : (value as UserStatus))
          }}
        >
          <option value="">全部状态</option>
          {USER_STATUS_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
    </div>
  )
}
