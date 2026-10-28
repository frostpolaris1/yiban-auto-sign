/**
 * **功能**
 * 账号筛选栏：手机号、状态、学校。
 *
 * **归属**
 * `features/accounts` 展示层：只受控渲染，不改筛选状态。
 *
 * **口径**
 * 手机号口径与契约一致：前 1–3 位、后 4 位或完整 11 位。
 * 学校筛选的数据源只对 master/admin 开放，其余角色置灰并给原因。
 */

import type { AccountStatus } from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import type { School } from '@/lib/api/school'
import { Icon } from '@/components/ui/Icon'
import { ACCOUNT_STATUS_OPTIONS } from './display'
import { can, denyReason } from './permissions'

export type AccountFilterValues = {
  status?: AccountStatus
  schoolId?: string
  q?: string
}

type AccountFiltersProps = {
  values: AccountFilterValues
  qInput: string
  role: Role | null
  schools: School[]
  onStatusChange: (status?: AccountStatus) => void
  onSchoolChange: (schoolId?: string) => void
  onQInputChange: (value: string) => void
}

export function AccountFilters({
  values,
  qInput,
  role,
  schools,
  onStatusChange,
  onSchoolChange,
  onQInputChange,
}: AccountFiltersProps) {
  const schoolReason = denyReason(role, 'schoolFilter')

  return (
    <div className="app-filters">
      <div className="input-icon app-filter-q">
        <span className="ico">
          <Icon name="search" />
        </span>
        <input
          className="input"
          type="search"
          aria-label="手机号筛选"
          placeholder="手机号：前 1–3 位 / 后 4 位 / 全 11 位"
          value={qInput}
          onChange={(event) => onQInputChange(event.target.value)}
        />
      </div>

      <div className="app-filter">
        <span className="app-filter-label">状态</span>
        <select
          className="select"
          aria-label="状态筛选"
          value={values.status ?? ''}
          onChange={(event) => {
            const value = event.target.value
            onStatusChange(value === '' ? undefined : (value as AccountStatus))
          }}
        >
          <option value="">全部状态</option>
          {ACCOUNT_STATUS_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="app-filter">
        <span className="app-filter-label">学校</span>
        <select
          className="select"
          aria-label="学校筛选"
          disabled={!can(role, 'schoolFilter')}
          value={values.schoolId ?? ''}
          onChange={(event) => {
            const value = event.target.value
            onSchoolChange(value === '' ? undefined : value)
          }}
        >
          <option value="">全部学校</option>
          {schools.map((school) => (
            <option key={school.id} value={school.id}>
              {school.name}
            </option>
          ))}
        </select>
        {schoolReason ? <span className="app-deny-note">{schoolReason}</span> : null}
      </div>
    </div>
  )
}
