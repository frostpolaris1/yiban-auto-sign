/**
 * **功能**
 * 窗口一览表：学校、任务、启停、窗口、来源与「查看计划」。
 *
 * **归属**
 * `features/schedule` 展示层：数据与回调由 `ScheduleView` 给。
 *
 * **口径**
 * 窗口只显示上海墙钟时分；start 或 end 缺一即视为平台未报窗口。
 * 平台未报窗口的行禁用「查看计划」：没有窗口就没有可预览的计划。
 * 行高沿用 `app-row-table` 的 48px 拍板值。
 * 行增删与重排走 180ms 过渡；不改行内按钮的点击语义。
 * 过渡库会永久给 tbody 写 `position: relative`：表体文字由次像素抗锯齿变为灰度。
 * 视觉基线按此口径拍，不是颜色或字号变化。
 */

import { useAutoAnimate } from '@formkit/auto-animate/react'
import type { ScheduleWindow } from '@/lib/api/schedule'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { formatClock } from '@/lib/format'
import { enabledLabel, enabledTone, sourceLabel, sourceTone } from './display'

type ScheduleWindowsTableProps = {
  rows: ScheduleWindow[]
  loading: boolean
  error: string | null
  onRetry: () => void
  onPreview: (row: ScheduleWindow, opener: HTMLElement) => void
}

function windowText(row: ScheduleWindow): string | null {
  if (!row.start || !row.end) return null
  return `${formatClock(row.start)}–${formatClock(row.end)}`
}

export function ScheduleWindowsTable({
  rows,
  loading,
  error,
  onRetry,
  onPreview,
}: ScheduleWindowsTableProps) {
  // 列表过渡：180ms 与 vendor 过渡同量级；尊重系统「减少动态效果」偏好。
  const [tbodyRef] = useAutoAnimate<HTMLTableSectionElement>({
    duration: 180,
    disrespectUserMotionPreference: false,
  })
  return (
    <div className="table-scroll">
      <table className="data-table app-row-table">
        <caption className="app-visually-hidden">时间片窗口</caption>
        <thead>
          <tr>
            <th scope="col">学校</th>
            <th scope="col">任务</th>
            <th scope="col">状态</th>
            <th scope="col">窗口</th>
            <th scope="col">来源</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody ref={tbodyRef}>
          {error ? (
            <tr>
              <td colSpan={6}>
                <span className="app-muted">{error}</span>{' '}
                <Button variant="ghost" size="sm" onClick={onRetry}>
                  重试
                </Button>
              </td>
            </tr>
          ) : null}

          {!error && loading && rows.length === 0 ? (
            <tr>
              <td colSpan={6} className="app-muted">
                加载中…
              </td>
            </tr>
          ) : null}

          {!error && !loading && rows.length === 0 ? (
            <tr>
              <td colSpan={6}>
                <div>当天没有任务窗口</div>
                <div className="app-muted">点「重新发现」向平台再取一次。</div>
              </td>
            </tr>
          ) : null}

          {rows.map((row) => {
            const text = windowText(row)
            return (
              <tr key={`${row.school_id}:${row.task_id}`}>
                <td>{row.school_name}</td>
                <td>
                  <div>{row.task_name}</div>
                  <div className="app-caption data-cell-mono">{row.task_id}</div>
                </td>
                <td>
                  <Badge tone={enabledTone(row.enabled)} dot>
                    {enabledLabel(row.enabled)}
                  </Badge>
                </td>
                <td>
                  {text === null ? (
                    <span className="app-muted">平台未报窗口</span>
                  ) : (
                    <span className="data-cell-mono">{text}</span>
                  )}
                </td>
                <td>
                  {row.source === null ? (
                    <span className="app-muted">—</span>
                  ) : (
                    <Badge tone={sourceTone(row.source)}>{sourceLabel(row.source)}</Badge>
                  )}
                </td>
                <td>
                  <div className="data-cell-actions app-row-actions">
                    <Button
                      variant="secondary"
                      size="sm"
                      aria-label={`查看计划 ${row.school_name}`}
                      disabled={text === null}
                      title={text === null ? '平台未报窗口，无法生成计划' : undefined}
                      onClick={(event) => onPreview(row, event.currentTarget)}
                    >
                      查看计划
                    </Button>
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
