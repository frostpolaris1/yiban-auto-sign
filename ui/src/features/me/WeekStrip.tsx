/**
 * **功能**
 * 近 7 天双载体条：形状（✓✕○－）＋文字 aria-label，不只靠颜色。
 *
 * **归属**
 * `features/me` 域内共享件：主页与账号概览共用同一口径。
 */

import { Link } from '@tanstack/react-router'
import { Icon } from '@/components/ui/Icon'
import type { SignCalendarDay } from '@/lib/api/me'
import { weekdayLabel } from './display'

function dayOutcome(day: SignCalendarDay): { mark: 'ok' | 'missed' | 'skipped' | 'pending' | 'none'; text: string } {
  if (day.total === 0) return { mark: 'none', text: '无记录' }
  if (day.success > 0) return { mark: 'ok', text: '成功' }
  if (day.failed > 0) return { mark: 'missed', text: '失败' }
  if (day.unknown > 0) return { mark: 'pending', text: '进行中' }
  return { mark: 'skipped', text: '未执行' }
}

export function WeekStrip({
  days,
  today,
  hideLink = false,
}: {
  days: SignCalendarDay[]
  today: string
  hideLink?: boolean
}) {
  if (days.length === 0) return null
  const recorded = days.filter((day) => day.total > 0)
  if (recorded.length === 0) return null
  const successDays = recorded.filter((day) => day.success > 0).length
  return (
    <section className="uapp-week" aria-label="近 7 天">
      <div className="uapp-week-head">
        <span>近 7 天</span>
        <div className="uapp-week-meta">
          <span>{successDays}/{recorded.length} 天成功</span>
          {hideLink ? null : (
            <Link className="uapp-week-link" to="/app/history">
              查看历史 ›
            </Link>
          )}
        </div>
      </div>
      <div className="uapp-week-days">
        {days.map((day) => {
          const outcome = dayOutcome(day)
          const isToday = day.day === today
          return (
            <div className="uapp-day" key={day.day}>
              <span
                className={
                  'uapp-day-cell uapp-day-cell--' +
                  outcome.mark +
                  (isToday ? ' uapp-day-cell--today' : '')
                }
                role="img"
                aria-label={`${weekdayLabel(day.day)}${isToday ? '，今天' : ''} · ${outcome.text}`}
              >
                {outcome.mark === 'ok' ? <Icon name="check" size={15} /> : null}
                {outcome.mark === 'missed' ? <Icon name="close" size={14} /> : null}
                {outcome.mark === 'skipped' ? <Icon name="trendFlat" size={14} /> : null}
                {outcome.mark === 'pending' ? <i className="uapp-day-pending-dot" aria-hidden="true" /> : null}
              </span>
              <span className="uapp-day-label">{isToday ? '今天' : weekdayLabel(day.day)}</span>
            </div>
          )
        })}
      </div>
    </section>
  )
}