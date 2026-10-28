/**
 * **功能**
 * 计划直方图：每片一根纯 CSS 柱，柱高按 count 与峰值的比例。
 *
 * **归属**
 * `features/schedule` 展示层：数据由预览模态给。
 *
 * **口径**
 * 不引图表库。柱高百分比由 `histogramBars` 算好。
 * 横轴是切片号；5 分钟一片；纵轴是账号数。
 */

import type { HistogramBar } from './drafts'

type ScheduleHistogramProps = {
  bars: HistogramBar[]
}

export function ScheduleHistogram({ bars }: ScheduleHistogramProps) {
  if (bars.length === 0) {
    return <p className="app-muted">没有可展示的时间片分布。</p>
  }

  return (
    <div className="app-histogram">
      <div
        className="app-histogram-bars"
        role="img"
        aria-label={`时间片分布，共 ${bars.length} 片`}
      >
        {bars.map((bar) => (
          <div
            key={bar.slice}
            className="app-histogram-col"
            title={`切片 ${bar.slice} · ${bar.count} 人`}
          >
            <span className="app-histogram-count">{bar.count}</span>
            <span className="app-histogram-track">
              <span
                className={`app-histogram-bar${bar.count === 0 ? ' is-empty' : ''}`}
                style={{ height: `${bar.heightPct}%` }}
              />
            </span>
            <span className="app-histogram-slice">{bar.slice}</span>
          </div>
        ))}
      </div>
      <p className="app-caption">横轴为切片号；5 分钟一片；纵轴为账号数。</p>
    </div>
  )
}
