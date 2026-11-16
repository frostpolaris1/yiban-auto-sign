/**
 * **功能**
 * 队列顶部遥测条：业务日、今日窗口、待审核、今日失败、今日未签五项概览。
 *
 * **归属**
 * `features/queue` 展示组件：只呈现与锚点跳转，不直接发请求。
 *
 * **口径**
 * 沿用正文默认字体与 `tabular-nums`，首次到位即从 0 滚动到目标值。
 * 移动端采用 6 列栅格双排水平平铺，大幅压缩纵向空间占用。
 */

import { Icon } from '@/components/ui/Icon'
import { QueueAnimatedNumber } from './QueueAnimatedNumber'
import { formatBusinessDayLabel, type WindowSummaryView } from './queueDisplay'

export type QueueTelemetryStripProps = {
  day: string
  windowSummary: WindowSummaryView | null
  windowError: boolean
  pendingTotal: number | null
  pendingDetail: string | null
  failedCount: number | null
  failedPartial: boolean
  unsignedCount: number | null
  unsignedPartial: boolean
  unsignedLabel: string
  onJumpTo: (groupId: string) => void
}

export function QueueTelemetryStrip({
  day,
  windowSummary,
  windowError,
  pendingTotal,
  pendingDetail,
  failedCount,
  failedPartial,
  unsignedCount,
  unsignedPartial,
  unsignedLabel,
  onJumpTo,
}: QueueTelemetryStripProps) {
  const dayLabel = formatBusinessDayLabel(day)

  return (
    <section className="q-telemetry" aria-label="今日待办摘要" data-queue-summary>
      <div className="q-tele-cell q-tele-meta">
        <span className="q-tele-label">
          <Icon name="calendar" size={13} />
          业务日
        </span>
        <div className="q-tele-value is-compact">{dayLabel.date}</div>
        <div className="q-tele-sub">{dayLabel.weekday || '今日'}</div>
      </div>

      <div className="q-tele-cell q-tele-meta">
        <span className="q-tele-label">
          <Icon name="clock" size={13} />
          今日窗口
        </span>
        <div className="q-tele-value is-compact">
          {windowError ? '读取失败' : windowSummary ? windowSummary.label : '—'}
        </div>
        <div className="q-tele-sub">
          {windowError
            ? '请稍后刷新'
            : windowSummary
              ? windowSummary.detail
              : '加载中…'}
        </div>
      </div>

      <button
        type="button"
        className="q-tele-cell q-tele-kpi is-clickable"
        onClick={() => onJumpTo('queue-group-pending')}
      >
        <span className="q-tele-label">
          <span className="q-dot is-warning" aria-hidden="true" />
          待审核
        </span>
        <div className={`q-tele-value ${pendingTotal && pendingTotal > 0 ? 'is-warning' : ''}`}>
          <QueueAnimatedNumber value={pendingTotal} />
        </div>
        <div className="q-tele-sub">{pendingDetail ?? '加载中…'}</div>
      </button>

      <button
        type="button"
        className="q-tele-cell q-tele-kpi is-clickable"
        onClick={() => onJumpTo('queue-group-failed')}
      >
        <span className="q-tele-label">
          <span className="q-dot is-danger" aria-hidden="true" />
          今日失败
        </span>
        <div className={`q-tele-value ${failedCount && failedCount > 0 ? 'is-danger' : ''}`}>
          {failedCount !== null && failedPartial ? '≥' : null}
          <QueueAnimatedNumber value={failedCount} />
        </div>
        <div className="q-tele-sub">
          {failedCount === null
            ? '加载中…'
            : failedCount === 0
              ? '暂无失败'
              : failedPartial
                ? '还有更多'
                : '可一键重试'}
        </div>
      </button>

      <button
        type="button"
        className="q-tele-cell q-tele-kpi is-clickable"
        onClick={() => onJumpTo('queue-group-unsigned')}
      >
        <span className="q-tele-label">
          <span className="q-dot is-info" aria-hidden="true" />
          {unsignedLabel}
        </span>
        <div className="q-tele-value">
          {unsignedCount !== null && unsignedPartial ? '≥' : null}
          <QueueAnimatedNumber value={unsignedCount} />
        </div>
        <div className="q-tele-sub">
          {unsignedCount === null
            ? '加载中…'
            : unsignedPartial
              ? '还有更多'
              : '查看明细'}
        </div>
      </button>
    </section>
  )
}
