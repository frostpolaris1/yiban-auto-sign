/**
 * **功能**
 * 顶部今日轮次摘要：总任务、已签、失败、在飞、待执行五个计数。
 *
 * **归属**
 * `features/run` 展示组件：只呈现计数，不取数。
 *
 * **口径**
 * 计数来自「今天、无筛选」的一页任务（limit 200，服务端缺省排序）。
 * `next_cursor` 非空说明当天超过一页：总数带「≥」前缀，其余计数
 * 同步标注「部分统计」，不假装全量。
 * 「在飞」＝running；「待执行」＝pending；已签含 success 与 skipped 之外的
 * 终态不再细分：徽章语义见运行页状态列。加载与失败由调用方给状态。
 */

import { Card } from '@/components/ui/Card'
import type { AdminTaskRow } from '@/lib/api/task'

export type RunCounts = {
  total: number
  success: number
  failed: number
  running: number
  pending: number
  /** true＝当天超过一页，计数只是已取到的部分。 */
  partial: boolean
}

/** 从一页任务行汇出五计数。未知状态不计入四类，只进总数。 */
export function runCounts(rows: AdminTaskRow[], partial: boolean): RunCounts {
  const counts: RunCounts = { total: rows.length, success: 0, failed: 0, running: 0, pending: 0, partial }
  for (const row of rows) {
    if (row.state === 'success') counts.success += 1
    else if (row.state === 'failed') counts.failed += 1
    else if (row.state === 'running') counts.running += 1
    else if (row.state === 'pending') counts.pending += 1
  }
  return counts
}

type RunSummaryProps = {
  counts: RunCounts | null
}

const CELLS: { key: keyof Omit<RunCounts, 'partial'>; label: string }[] = [
  { key: 'total', label: '总任务' },
  { key: 'success', label: '已签' },
  { key: 'failed', label: '失败' },
  { key: 'running', label: '在飞' },
  { key: 'pending', label: '待执行' },
]

export function RunSummary({ counts }: RunSummaryProps) {
  return (
    <Card eyebrow="Run · 今日" title="今日轮次摘要">
      <dl className="run-summary">
        {CELLS.map((cell) => (
          <div className="run-summary-cell" key={cell.key}>
            <dt>{cell.label}</dt>
            <dd>
              {counts === null
                ? '—'
                : `${counts.partial && cell.key === 'total' ? '≥' : ''}${counts[cell.key]}`}
            </dd>
          </div>
        ))}
      </dl>
      {counts?.partial ? (
        <p className="app-caption">当天任务超过一页，计数只统计已取到的前 200 条。</p>
      ) : null}
    </Card>
  )
}
