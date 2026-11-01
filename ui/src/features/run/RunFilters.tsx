/**
 * **功能**
 * 运行页筛选条：业务日、任务状态、学校下拉、检索词与导出按钮。
 *
 * **归属**
 * `features/run` 展示组件：只呈现控件，不取数。
 *
 * **口径**
 * 标签用 UX-0 的 FilterField：label htmlFor 与 aria-describedby 由它合并。
 * 日输入是原生 date；空值表示「业务日今天」，键不进 URL。
 * 学校是下拉：选项来自 `GET /api/schools`（master 全量、admin 绑定范围），
 * URL 存学校 id——自由文本 UUID 会在服务端回 400，也把内部 id 暴露成输入面。
 * 检索词按学校/任务名子串或 11 位完整手机号匹配；回车立即生效。
 * 筛选写回一律由调用方（RunView → 路由）完成，本组件不发请求。
 */

import { FilterField } from '@/components/ui/FilterField'
import { BusyButton } from '@/components/ui/BusyButton'
import type { School } from '@/lib/api/school'
import { TASK_STATES, TASK_STATE_LABELS } from '@/lib/identity'
import type { RunStateFilter, RunURLState } from './urlState'

export type RunFiltersProps = {
  values: RunURLState
  qInput: string
  exportBusy: boolean
  schools: School[]
  schoolsLoading: boolean
  onDayChange: (day: string | undefined) => void
  onStateChange: (state: RunStateFilter | undefined) => void
  onSchoolChange: (school: string | undefined) => void
  onQInputChange: (q: string) => void
  onQCommit: () => void
  onExport: () => void
}

export function RunFilters({
  values,
  qInput,
  exportBusy,
  schools,
  schoolsLoading,
  onDayChange,
  onStateChange,
  onSchoolChange,
  onQInputChange,
  onQCommit,
  onExport,
}: RunFiltersProps) {
  return (
    <div className="app-filters">
      <FilterField id="run-filter-day" label="业务日">
        <input
          className="input"
          type="date"
          value={values.day ?? ''}
          onChange={(event) => onDayChange(event.target.value === '' ? undefined : event.target.value)}
        />
      </FilterField>

      <FilterField id="run-filter-state" label="状态筛选">
        <select
          className="select"
          value={values.state ?? ''}
          onChange={(event) => {
            const value = event.target.value
            onStateChange(value === '' ? undefined : (value as RunStateFilter))
          }}
        >
          <option value="">全部状态</option>
          {TASK_STATES.map((value) => (
            <option key={value} value={value}>
              {TASK_STATE_LABELS[value]}
            </option>
          ))}
        </select>
      </FilterField>

      <FilterField
        id="run-filter-school"
        label="学校"
        hint={schoolsLoading ? '学校列表读取中…' : undefined}
      >
        <select
          className="select"
          value={values.school ?? ''}
          onChange={(event) => onSchoolChange(event.target.value === '' ? undefined : event.target.value)}
        >
          <option value="">全部学校</option>
          {schools.map((school) => (
            <option key={school.id} value={school.id}>
              {school.name}
            </option>
          ))}
        </select>
      </FilterField>

      <FilterField
        id="run-filter-q"
        label="检索"
        hint="学校或任务名的一段，或 11 位完整手机号"
      >
        <input
          className="input"
          type="search"
          placeholder="学校/任务名，或 11 位完整手机号"
          value={qInput}
          onChange={(event) => onQInputChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return
            event.preventDefault()
            onQCommit()
          }}
        />
      </FilterField>

      <BusyButton variant="secondary" busy={exportBusy} busyLabel="导出中…" onClick={onExport}>
        导出 CSV
      </BusyButton>
    </div>
  )
}
