/**
 * **功能**
 * 运行页筛选条：业务日、任务状态、学校、检索词与导出按钮。
 *
 * **归属**
 * `features/run` 展示组件：只呈现控件，不取数。
 *
 * **口径**
 * 标签用 UX-0 的 FilterField：label htmlFor 与 aria-describedby 由它合并。
 * 日输入是原生 date；空值表示「业务日今天」，键不进 URL。
 * 学校与检索词是自由文本；检索词回车立即生效。
 * 筛选写回一律由调用方（RunView → 路由）完成，本组件不发请求。
 */

import { FilterField } from '@/components/ui/FilterField'
import { BusyButton } from '@/components/ui/BusyButton'
import { TASK_STATES, TASK_STATE_LABELS } from '@/lib/identity'
import type { RunStateFilter, RunURLState } from './urlState'

export type RunFiltersProps = {
  values: RunURLState
  qInput: string
  exportBusy: boolean
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

      <FilterField id="run-filter-school" label="学校">
        <input
          className="input"
          type="search"
          placeholder="学校 ID"
          value={values.school ?? ''}
          onChange={(event) => onSchoolChange(event.target.value === '' ? undefined : event.target.value)}
        />
      </FilterField>

      <FilterField id="run-filter-q" label="检索">
        <input
          className="input"
          type="search"
          placeholder="手机号前缀或学校名，回车立即查询"
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
