/**
 * **功能**
 * 中部任务时间线：按学校泳道分组，一行一次签到尝试。
 *
 * **归属**
 * `features/run` 展示组件：只呈现分组与行，不取数。
 *
 * **口径**
 * 分组保持服务端序（run_at 倒序）：学校按首次出现的行序排，
 * 泳道内行不再排序，两次渲染同数据同序。
 * 行内时间是上海墙钟 HH:mm（与页面其余时间同口径）。
 * 状态徽章用 `lib/identity` 的共用词。诊断按钮是原生 button：
 * 键盘可达，Enter/Space 由浏览器语义给出，不另写全局监听。
 */

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import type { AdminTaskRow } from '@/lib/api/task'
import { formatClock } from '@/lib/format'
import { runStateLabel, runStateTone } from './display'

export type SchoolLane = {
  schoolId: string
  schoolName: string
  rows: AdminTaskRow[]
}

/** 按学校分组；学校按首次出现序，泳道内保持行序。 */
export function lanesBySchool(rows: AdminTaskRow[]): SchoolLane[] {
  const lanes: SchoolLane[] = []
  const index = new Map<string, number>()
  for (const row of rows) {
    const at = index.get(row.school_id)
    if (at === undefined) {
      index.set(row.school_id, lanes.length)
      lanes.push({ schoolId: row.school_id, schoolName: row.school_name, rows: [row] })
      continue
    }
    lanes[at].rows.push(row)
  }
  return lanes
}

type SchoolLanesProps = {
  lanes: SchoolLane[]
  onOpenDiagnostics: (row: AdminTaskRow, opener: HTMLElement) => void
}

export function SchoolLanes({ lanes, onOpenDiagnostics }: SchoolLanesProps) {
  return (
    <div className="run-lanes">
      {lanes.map((lane) => (
        <section className="run-lane" key={lane.schoolId} aria-label={`泳道 ${lane.schoolName}`}>
          <header className="run-lane-head">
            <h3>{lane.schoolName}</h3>
            <span className="app-caption">{lane.rows.length} 项</span>
          </header>
          <ul className="run-lane-rows">
            {lane.rows.map((row) => (
              <li className="run-task-row" key={row.id}>
                <time dateTime={row.run_at}>{formatClock(row.run_at)}</time>
                <Badge tone={runStateTone(row.state)}>{runStateLabel(row.state)}</Badge>
                <span className="run-task-account">{row.account_phone_masked}</span>
                <span className="run-task-name">{row.task_name}</span>
                {row.attempts > 1 ? (
                  <span className="app-caption">尝试 {row.attempts} 次</span>
                ) : null}
                {row.result ? <span className="run-task-result">{row.result}</span> : null}
                <Button
                  variant="outline-primary"
                  size="sm"
                  onClick={(event) => onOpenDiagnostics(row, event.currentTarget)}
                >
                  诊断
                </Button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
