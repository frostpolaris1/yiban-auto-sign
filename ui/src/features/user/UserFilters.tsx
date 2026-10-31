/**
 * **功能**
 * 用户筛选栏：邮箱前缀、角色、状态、含已删除开关。
 *
 * **归属**
 * `features/user` 展示层：只受控渲染，不改筛选状态。
 *
 * **口径**
 * 三个控件同属一行，布局复用 `.app-filters`。
 * 每个控件用 `FilterField` 壳：标签与描述关联由壳合并注入（UX-0 首个业务采用）。
 * 邮箱前缀是 `GET /api/users` 的 `q`：只匹配地址开头。
 * 回车由调用方立即生效；输入防抖也在调用方。
 * 「含已删除」是布尔开关：开＝`include_deleted=true`，关＝键不存在。
 */

import type { UserRole, UserStatus } from '@/lib/api/user'
import { FilterField } from '@/components/ui/FilterField'
import { USER_ROLE_OPTIONS, USER_STATUS_OPTIONS } from './display'
import type { UserURLState } from './urlState'

type UserFiltersProps = {
  values: UserURLState
  qInput: string
  onRoleChange: (role?: UserRole) => void
  onStatusChange: (status?: UserStatus) => void
  onDeletedChange: (deleted: boolean) => void
  onQInputChange: (value: string) => void
  onQCommit: () => void
}

export function UserFilters({
  values,
  qInput,
  onRoleChange,
  onStatusChange,
  onDeletedChange,
  onQInputChange,
  onQCommit,
}: UserFiltersProps) {
  return (
    <div className="app-filters">
      <FilterField id="user-filter-q" label="邮箱前缀筛选">
        <input
          className="input"
          type="search"
          placeholder="邮箱前缀，回车立即查询"
          value={qInput}
          onChange={(event) => onQInputChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return
            event.preventDefault()
            onQCommit()
          }}
        />
      </FilterField>

      <FilterField id="user-filter-role" label="角色筛选">
        <select
          className="select"
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
      </FilterField>

      <FilterField id="user-filter-status" label="状态筛选">
        <select
          className="select"
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
      </FilterField>

      <div className="app-filter">
        <label className="app-filter-check">
          <input
            type="checkbox"
            checked={values.deleted ?? false}
            onChange={(event) => onDeletedChange(event.target.checked)}
          />
          含已删除
        </label>
      </div>
    </div>
  )
}
