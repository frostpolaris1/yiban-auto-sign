/**
 * **功能**
 * 用户端历史：月历、日详情与 30 天回看。
 *
 * **归属**
 * `features/me` 域容器：取数、月份网格与日详情在此。
 *
 * **口径**
 * 月范围一次请求 ≤31 天；未来日不可点，日详情为空。
 * 日格不只靠颜色：aria-label 同时写日期与结果。
 * 空日写「当天没有任务」，不写「暂无数据」。
 * 不展示内部 reason/suggestion；日明细只有时间、身份、状态、尝试次数。
 * 桌面左右分栏；手机上下堆叠。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Icon } from '@/components/ui/Icon'
import { formatClock, parseDay } from '@/lib/format'
import type { SignCalendarAccount, SignCalendarDay } from '@/lib/api/me'
import { displayName } from '@/lib/account-display'
import { useMyAccounts, useSignCalendar, useSignCalendarDay } from './queries'
import { calendarDayTone, todayKey } from './display'

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日'] as const

const monthFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: 'long',
})

const dayFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  month: 'long',
  day: 'numeric',
  weekday: 'short',
})

function iso(date: Date): string {
  return date.toISOString().slice(0, 10)
}

function monthOf(day: string): string {
  return day.slice(0, 7)
}

function monthRange(month: string): { from: string; to: string; label: string } {
  const [year, m] = month.split('-').map(Number)
  const first = new Date(Date.UTC(year, m - 1, 1))
  const last = new Date(Date.UTC(year, m, 0))
  return {
    from: iso(first),
    to: iso(last),
    label: monthFormatter.format(new Date(`${month}-01T12:00:00+08:00`)),
  }
}

function shiftMonth(month: string, delta: number): string {
  const [year, m] = month.split('-').map(Number)
  const date = new Date(Date.UTC(year, m - 1 + delta, 1))
  return iso(date).slice(0, 7)
}

/** 固定 6 周 × 7 天，周一开始。 */
function buildCells(month: string): string[] {
  const [year, m] = month.split('-').map(Number)
  const first = new Date(Date.UTC(year, m - 1, 1))
  const offset = (first.getUTCDay() + 6) % 7
  const start = new Date(Date.UTC(year, m - 1, 1 - offset))
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate() + index))
    return iso(date)
  })
}

function dayLabel(day: string): string {
  const date = new Date(`${day}T12:00:00+08:00`)
  if (Number.isNaN(date.getTime())) return day
  return dayFormatter.format(date)
}

function cellTone(info: SignCalendarDay | undefined, future: boolean): string {
  switch (calendarDayTone(info, future)) {
    case 'danger':
      return 'uapp-cal-dot uapp-cal-dot--missed'
    case 'success':
      return 'uapp-cal-dot'
    case 'pending':
    case 'warning':
      return 'uapp-cal-dot uapp-cal-dot--warning'
    default:
      return 'uapp-cal-dot uapp-cal-dot--empty'
  }
}

function cellAria(day: string, info: SignCalendarDay | undefined, future: boolean): string {
  if (future) return `${dayLabel(day)}，未来`
  if (!info || info.total === 0) return `${dayLabel(day)}，无记录`
  const parts: string[] = []
  if (info.failed > 0) parts.push(info.failed + ' 个失败')
  if (info.success > 0) parts.push(info.success + ' 个成功')
  if (info.skipped > 0) parts.push(info.skipped + ' 个未执行')
  if (info.unknown > 0) parts.push(info.unknown + ' 个待确认')
  return `${dayLabel(day)}，${parts.join('，')}`
}

function accountName(
  item: SignCalendarAccount,
  accounts: Array<{ id: string; name: string; phone_masked: string }>,
): string {
  const account = accounts.find((entry) => entry.id === item.account_id)
  return account ? displayName(account.name, account.phone_masked) : '账号'
}

function stateLabel(state: SignCalendarAccount['state']): string {
  switch (state) {
    case 'success':
      return '成功'
    case 'failed':
      return '失败'
    case 'skipped':
      return '未执行'
    default:
      return '待确认'
  }
}

function stateClass(state: SignCalendarAccount['state']): string {
  switch (state) {
    case 'success':
      return 'uapp-status uapp-status--success'
    case 'failed':
      return 'uapp-status uapp-status--danger'
    case 'skipped':
      return 'uapp-status uapp-status--warning'
    default:
      return 'uapp-status uapp-status--info'
  }
}

export function HistoryView({
  day,
  onDayChange,
}: {
  day: string | undefined
  onDayChange: (day: string) => void
}) {
  const today = todayKey(new Date())
  const selected = day && parseDay(day) ? day : today
  const [month, setMonth] = useState(() => monthOf(selected))
  const lastSelected = useRef(selected)

  useEffect(() => {
    if (lastSelected.current !== selected) {
      lastSelected.current = selected
      if (monthOf(selected) !== month) setMonth(monthOf(selected))
    }
  }, [selected, month])

  const range = useMemo(() => monthRange(month), [month])
  const cells = useMemo(() => buildCells(month), [month])
  const monthQuery = useSignCalendar(range.from, range.to)
  const dayQuery = useSignCalendarDay(selected)
  const accountsQuery = useMyAccounts()

  const dayMap = useMemo(() => {
    const map = new Map<string, SignCalendarDay>()
    for (const item of monthQuery.data?.days ?? []) map.set(item.day, item)
    return map
  }, [monthQuery.data])

  const accounts = accountsQuery.data?.items ?? []
  const monthStats = useMemo(() => {
    let success = 0
    let failed = 0
    for (const item of monthQuery.data?.days ?? []) {
      if (item.success > 0) success += 1
      if (item.failed > 0) failed += 1
    }
    return { success, failed }
  }, [monthQuery.data])

  const goPrev = () => setMonth(shiftMonth(month, -1))
  const goNext = () => setMonth(shiftMonth(month, 1))
  // 不翻到未来月份；未来的日格也不可点。
  const canGoNext = shiftMonth(month, 1) <= monthOf(today)

  const pick = (next: string) => {
    const future = next > today
    if (future) return
    if (monthOf(next) !== month) setMonth(monthOf(next))
    onDayChange(next)
  }

  return (
    <div className="uapp-page uapp-page--history">
      <div className="uapp-history-head">
        <div className="uapp-home-eyebrow">按月回看签到记录</div>
        <h1 className="uapp-greeting">历史</h1>
      </div>

      <div className="uapp-history-grid">
        <section className="uapp-card uapp-calendar" aria-label="签到日历">
          <div className="uapp-cal-head">
            <button
              type="button"
              className="uapp-icon-btn"
              aria-label="上个月"
              onClick={goPrev}
            >
              <Icon name="chevronLeft" size={18} />
            </button>
            <div className="uapp-cal-title">
              <strong>{range.label}</strong>
              <span className="uapp-cal-stats">
                {monthStats.success} 天成功 · {monthStats.failed} 天失败
              </span>
            </div>
            <button
              type="button"
              className="uapp-icon-btn"
              aria-label="下个月"
              disabled={!canGoNext}
              onClick={goNext}
            >
              <Icon name="chevronRight" size={18} />
            </button>
          </div>

          <div className="uapp-cal-weekdays" aria-hidden="true">
            {WEEKDAYS.map((label) => (
              <span key={label}>{label}</span>
            ))}
          </div>

          {monthQuery.isError ? (
            <div className="uapp-alert-block">
              <Alert tone="danger">日历读取失败。</Alert>
              <button
                type="button"
                className="uapp-card-action"
                onClick={() => void monthQuery.refetch()}
              >
                重试
              </button>
            </div>
          ) : (
            <div className="uapp-cal-grid" aria-busy={monthQuery.isPending}>
              {cells.map((cell) => {
                const inMonth = monthOf(cell) === month
                const info = dayMap.get(cell)
                const future = cell > today
                const isSelected = cell === selected
                const isToday = cell === today
                return (
                  <button
                    key={cell}
                    type="button"
                    className={[
                      'uapp-cal-cell',
                      !inMonth && 'is-outside',
                      isSelected && 'is-selected',
                      isToday && 'is-today',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    aria-label={cellAria(cell, info, future)}
                    aria-pressed={isSelected}
                    aria-current={isToday ? 'date' : undefined}
                    aria-disabled={future || undefined}
                    onClick={() => pick(cell)}
                  >
                    <span className="uapp-cal-day">{Number(cell.slice(8))}</span>
                    {!future ? <span className={cellTone(info, future)} /> : null}
                  </button>
                )
              })}
            </div>
          )}

          <div className="uapp-cal-legend">
            <span><span className="uapp-cal-dot" />成功</span>
            <span><span className="uapp-cal-dot uapp-cal-dot--missed" />失败</span>
            <span><span className="uapp-cal-dot uapp-cal-dot--empty" />无记录</span>
          </div>
        </section>

        <section className="uapp-card uapp-day-detail" aria-label="当天明细">
          <div className="uapp-day-hero">
            <span className="uapp-day-big">{Number(selected.slice(8))}</span>
            <div className="uapp-day-hero-text">
              <h2 className="uapp-card-title">{dayLabel(selected)}</h2>
              <p className="uapp-card-sub">
                {monthOf(selected) !== month
                  ? '所选日不在本月'
                  : dayQuery.isPending
                    ? selected === today ? '今天' : '读取中…'
                    : dayQuery.isError
                      ? '读取失败'
                      : `${selected === today ? '今天 · ' : ''}${dayQuery.data?.accounts.length ?? 0} 条记录`}
              </p>
            </div>
            <div className="uapp-day-hero-side">
              {selected !== today ? (
                <button
                  type="button"
                  className="uapp-hero-link uapp-history-today-btn"
                  onClick={() => pick(today)}
                >
                  回到今天
                </button>
              ) : null}
              {dayQuery.data && !dayQuery.data.future && dayQuery.data.accounts.length > 0 ? (
                <span
                  className={
                    dayQuery.data.accounts.some((a) => a.state === 'failed')
                      ? 'uapp-status uapp-status--danger'
                      : 'uapp-status uapp-status--success'
                  }
                >
                  <span className="uapp-status-dot" />
                  {dayQuery.data.accounts.some((a) => a.state === 'failed') ? '有失败' : '全部成功'}
                </span>
              ) : null}
            </div>
          </div>

          {dayQuery.isPending ? (
            <div className="uapp-skeleton-stack" aria-busy="true" aria-label="加载中">
              <div className="uapp-skeleton uapp-skeleton-row" />
              <div className="uapp-skeleton uapp-skeleton-row" />
            </div>
          ) : dayQuery.isError ? (
            <div>
              <p className="uapp-card-sub">当天记录读取失败。</p>
              <button
                type="button"
                className="uapp-card-action"
                onClick={() => void dayQuery.refetch()}
              >
                重试
              </button>
            </div>
          ) : dayQuery.data.future ? (
            <p className="uapp-card-sub">这一天还没有到。</p>
          ) : dayQuery.data.accounts.length === 0 ? (
            <p className="uapp-card-sub">当天没有任务。</p>
          ) : (
            <div className="uapp-rows">
              {dayQuery.data.accounts.map((item) => (
                <div className="uapp-row" key={item.account_id}>
                  <span className="uapp-row-icon">
                    <Icon name="user" size={16} />
                  </span>
                  <span className="uapp-row-main">
                    <span className="uapp-row-label">
                      {accountName(item, accounts)}
                    </span>
                    <span className="uapp-row-sub">
                      {item.run_at ? `执行时间 ${formatClock(item.run_at)} · ` : ''}
                      尝试 {item.attempts} 次
                    </span>
                  </span>
                  <span className={stateClass(item.state)}>
                    <span className="uapp-status-dot" />
                    {stateLabel(item.state)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
