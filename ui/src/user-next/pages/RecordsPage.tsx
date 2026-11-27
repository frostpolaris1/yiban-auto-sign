/**
 * **功能**
 * 用户端「记录」页：月历、当天明细与回看。
 *
 * **归属**
 * user-next/pages；查询复用 features/me/queries。
 *
 * **口径**
 * 月历只请求当月；未来日不可点。日格同时用文字与色点表达结果。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { formatClock, parseDay } from '@/lib/format'
import { displayName } from '@/lib/account-display'
import type { SignCalendarAccount, SignCalendarDay } from '@/lib/api/me'
import {
  useMyAccounts,
  useSignCalendar,
  useSignCalendarDay,
} from '@/features/me/queries'
import { todayKey } from '@/features/me/display'
import { cx } from '@/lib/cx'

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
  weekday: 'long',
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
    label: monthFormatter.format(new Date(month + '-01T12:00:00+08:00')),
  }
}

function shiftMonth(month: string, delta: number): string {
  const [year, m] = month.split('-').map(Number)
  const date = new Date(Date.UTC(year, m - 1 + delta, 1))
  return iso(date).slice(0, 7)
}

function buildCells(month: string): string[] {
  const [year, m] = month.split('-').map(Number)
  const first = new Date(Date.UTC(year, m - 1, 1))
  const offset = (first.getUTCDay() + 6) % 7
  const start = new Date(Date.UTC(year, m - 1, 1 - offset))
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(
      Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate() + index),
    )
    return iso(date)
  })
}

function dayLabel(day: string): string {
  const date = new Date(day + 'T12:00:00+08:00')
  if (Number.isNaN(date.getTime())) return day
  return dayFormatter.format(date).replace(/(日)(星期)/, '$1 $2')
}

function toneOf(info: SignCalendarDay | undefined, future: boolean): string {
  if (future || !info || info.total === 0) return 'none'
  if (info.failed > 0) return 'danger'
  if (info.success > 0) return 'success'
  return 'warning'
}

function cellAria(
  day: string,
  info: SignCalendarDay | undefined,
  future: boolean,
): string {
  if (future) return dayLabel(day) + '，未来'
  if (!info || info.total === 0) return dayLabel(day) + '，无记录'
  if (info.failed > 0) {
    const parts = [info.failed + ' 个失败']
    if (info.success > 0) parts.push(info.success + ' 个成功')
    if (info.skipped > 0) parts.push(info.skipped + ' 个未执行')
    return dayLabel(day) + '，' + parts.join('，')
  }
  if (info.success > 0) return dayLabel(day) + '，' + info.success + ' 个成功'
  return dayLabel(day) + '，' + info.total + ' 个未执行'
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

function stateTone(state: SignCalendarAccount['state']): string {
  switch (state) {
    case 'success':
      return 'success'
    case 'failed':
      return 'danger'
    case 'skipped':
      return 'warning'
    default:
      return 'blue'
  }
}

export function RecordsPage({
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
  let success = 0
  let failed = 0
  for (const item of monthQuery.data?.days ?? []) {
    if (item.success > 0 && item.failed === 0) success += 1
    if (item.failed > 0) failed += 1
  }

  const canGoNext = shiftMonth(month, 1) <= monthOf(today)

  const pick = (next: string) => {
    if (next > today) return
    if (monthOf(next) !== month) setMonth(monthOf(next))
    onDayChange(next)
  }

  const dayAccounts = dayQuery.data?.accounts ?? []
  const dayFailed = dayAccounts.some((item) => item.state === 'failed')
  const gridRef = useRef<HTMLDivElement>(null)

  const moveFocus = (event: KeyboardEvent<HTMLDivElement>, offset: number) => {
    const current = (event.target as HTMLElement).dataset.day
    if (!current) return
    const date = new Date(current + 'T00:00:00Z')
    date.setUTCDate(date.getUTCDate() + offset)
    const next = date.toISOString().slice(0, 10)
    const target = gridRef.current?.querySelector<HTMLButtonElement>(
      `[data-day="${next}"]`,
    )
    if (!target || target.getAttribute('aria-disabled') === 'true') return
    event.preventDefault()
    target.focus()
  }

  const onGridKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') moveFocus(event, -1)
    else if (event.key === 'ArrowRight') moveFocus(event, 1)
    else if (event.key === 'ArrowUp') moveFocus(event, -7)
    else if (event.key === 'ArrowDown') moveFocus(event, 7)
    else if (event.key === 'Home') moveFocus(event, -((new Date((event.target as HTMLElement).dataset.day + 'T00:00:00Z').getUTCDay() + 6) % 7))
    else if (event.key === 'End') moveFocus(event, 6 - ((new Date((event.target as HTMLElement).dataset.day + 'T00:00:00Z').getUTCDay() + 6) % 7))
  }

  return (
    <div className="un-records">
      <header className="un-records__head">
        <div className="un-records__title-row">
          <h1 className="un-records__title">签到记录</h1>
          <p className="un-records__note">按月翻看；失败会写清时间与尝试次数。</p>
        </div>
      </header>

      <div className="un-records__grid">
        <section className="un-cal" aria-label="签到日历">
          <div className="un-cal__toolbar">
            <button
              type="button"
              className="un-cal__nav"
              aria-label="上个月"
              onClick={() => setMonth(shiftMonth(month, -1))}
            >
              ←
            </button>
            <div className="un-cal__heading">
              <h2 className="un-cal__month">{range.label}</h2>
              <p className="un-cal__stats">
                当月 {success} 天成功 <span aria-hidden="true">·</span> {failed} 天失败
              </p>
            </div>
            <button
              type="button"
              className="un-cal__nav"
              aria-label="下个月"
              disabled={!canGoNext}
              onClick={() => setMonth(shiftMonth(month, 1))}
            >
              →
            </button>
          </div>

          <div className="un-cal__weekdays" aria-hidden="true">
            {WEEKDAYS.map((label) => (
              <span key={label}>{label}</span>
            ))}
          </div>

          {monthQuery.isError ? (
            <div className="un-cal__error" role="alert">
              <p>日历读取失败。</p>
              <button
                type="button"
                className="un-btn un-btn--secondary un-btn--sm"
                onClick={() => void monthQuery.refetch()}
              >
                重试
              </button>
            </div>
          ) : (
            <div
              ref={gridRef}
              className="un-cal__grid"
              aria-busy={monthQuery.isPending}
              onKeyDown={onGridKeyDown}
            >
              {cells.map((cell) => {
                const inMonth = monthOf(cell) === month
                const info = dayMap.get(cell)
                const future = cell > today
                const isSelected = cell === selected
                const isToday = cell === today
                const tone = toneOf(info, future)
                return (
                  <button
                    key={cell}
                    type="button"
                    className={cx(
                      'un-cal__cell',
                      !inMonth && 'is-outside',
                      isSelected && 'is-selected',
                      isToday && 'is-today',
                    )}
                    aria-label={cellAria(cell, info, future)}
                    aria-pressed={isSelected}
                    aria-current={isToday ? 'date' : undefined}
                    aria-disabled={future || undefined}
                    data-day={cell}
                    tabIndex={isSelected ? 0 : -1}
                    onClick={() => pick(cell)}
                  >
                    <span className="un-cal__day">
                      {Number(cell.slice(8))}
                    </span>
                    {!future ? (
                      <span className={cx('un-cal__dot', 'is-' + tone)} aria-hidden="true" />
                    ) : null}
                  </button>
                )
              })}
            </div>
          )}

          <div className="un-cal__legend">
            <span><i className="un-cal__dot is-success" aria-hidden="true" />成功</span>
            <span><i className="un-cal__dot is-danger" aria-hidden="true" />失败</span>
            <span><i className="un-cal__dot is-warning" aria-hidden="true" />未执行</span>
            <span><i className="un-cal__dot is-none" aria-hidden="true" />无记录</span>
          </div>
        </section>

        <section className="un-day-card" aria-label="当天明细">
          <header className="un-day-card__head">
            <div>
              <p className="un-day-card__eyebrow">
                {selected === today ? '今天' : '所选日期'}
              </p>
              <h2 className="un-day-card__title">{dayLabel(selected)}</h2>
            </div>
            <span className="un-day-card__big un-serif" aria-hidden="true">
              {Number(selected.slice(8))}
            </span>
          </header>

          <div className="un-day-card__meta">
            {monthOf(selected) !== month ? (
              <span>所选日不在本月</span>
            ) : dayQuery.isPending ? (
              <span>读取中…</span>
            ) : dayQuery.isError ? (
              <span>读取失败</span>
            ) : dayQuery.data?.future ? (
              <span>这一天还没有到。</span>
            ) : dayAccounts.length === 0 ? (
              <span>当天没有任务。</span>
            ) : (
              <>
                <span>{dayAccounts.length} 条记录</span>
                {dayFailed ? (
                  <span className="un-chip un-chip--danger">有失败</span>
                ) : null}
              </>
            )}
          </div>

          {dayQuery.isError ? (
            <div className="un-day-card__error">
              <button
                type="button"
                className="un-btn un-btn--secondary un-btn--sm"
                onClick={() => void dayQuery.refetch()}
              >
                重试
              </button>
            </div>
          ) : dayQuery.isPending ? (
            <div className="un-day-card__skeleton" aria-busy="true" aria-label="加载中">
              <span className="un-skeleton" />
              <span className="un-skeleton" />
            </div>
          ) : dayAccounts.length === 0 ? (
            <p className="un-day-card__empty">
              {dayQuery.data?.future
                ? '到这一天，系统会按窗口自动执行。'
                : '没有执行记录；如果当天在窗口内，系统会自动开始。'}
            </p>
          ) : (
            <ol className="un-day-list">
              {dayAccounts.map((item) => (
                <li className="un-day-list__item" key={item.account_id}>
                  <span className={cx('un-day-list__rail', 'is-' + stateTone(item.state))} aria-hidden="true" />
                  <div className="un-day-list__main">
                    <p className="un-day-list__name">{accountName(item, accounts)}</p>
                    <p className="un-day-list__time">
                      {item.run_at ? '执行 ' + formatClock(item.run_at) : '未记录执行时间'}
                      <span aria-hidden="true"> · </span>
                      尝试 {item.attempts} 次
                    </p>
                  </div>
                  <span className={cx('un-chip', 'un-chip--' + stateTone(item.state))}>
                    {stateLabel(item.state)}
                  </span>
                </li>
              ))}
            </ol>
          )}

          {selected !== today ? (
            <button
              type="button"
              className="un-day-card__today"
              onClick={() => pick(today)}
            >
              回到今天 <span aria-hidden="true">→</span>
            </button>
          ) : null}
        </section>
      </div>
    </div>
  )
}
