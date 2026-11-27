/**
 * **功能**
 * 用户端「时间偏好」页：查看有效窗口，选择 5 分钟档，保存或恢复自动。
 *
 * **归属**
 * user-next/pages；取数与保存走 features/me 查询与 schedule API。
 *
 * **口径**
 * 必须同时显示窗口与绝对时刻；读取失败禁用写入，避免覆盖未知值。
 * 时间片只负责偏好，不承诺平台执行时刻。
 */

import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { notifySuccess } from '@/components/ui/toast'
import { ApiError } from '@/lib/api/client'
import { setTimePref } from '@/lib/api/schedule'
import type { TimePref } from '@/lib/api/schedule'
import { ME_KEYS, useTimePref } from '@/features/me/queries'

type Slot = NonNullable<TimePref['slots']>[number]

function prefErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '时间片不在有效窗口内。'
    if (error.status === 403) return '此部署未开放时间偏好。'
    if (error.status === 429) return '操作太频繁，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    return '保存失败，请稍后重试。'
  }
  return '网络异常，请稍后重试。'
}

function slotNote(slot: Slot): string {
  if (slot.pct >= 100) return '满员'
  if (slot.pct >= 80) return '紧张'
  return ''
}

export function TimePrefPage() {
  const queryClient = useQueryClient()
  const prefQuery = useTimePref()
  const pref = prefQuery.data
  const stored = pref?.offset_min ?? null
  const [draft, setDraft] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const rulerRef = useRef<HTMLDivElement | null>(null)
  const centeredOnce = useRef(false)
  const [overflowing, setOverflowing] = useState(false)
  const [atStart, setAtStart] = useState(true)
  const [atEnd, setAtEnd] = useState(false)

  const slots = useMemo(() => pref?.slots ?? [], [pref])
  const allowed = pref?.allowed ?? true
  const selected = draft ?? stored
  const selectedSlot = slots.find((slot) => slot.offset_min === selected) ?? null
  const windowStart = pref?.window?.start ?? null
  const windowEnd = pref?.window?.end ?? null
  const endShown = slots.some((slot) => slot.label === windowEnd)

  const saveMutation = useMutation({
    mutationFn: (offset: number | null) => setTimePref({ offset_min: offset }),
    onSuccess: (data, offset) => {
      queryClient.setQueryData(ME_KEYS.timePref, data)
      setDraft(null)
      setError(null)
      const saved = data.slots?.find((slot) => slot.offset_min === offset)?.label ?? ''
      notifySuccess(offset === null ? '已恢复自动分配' : '已保存 ' + saved)
    },
    onError: (err) => setError(prefErrorText(err)),
  })

  const canSave = allowed && draft !== null && draft !== stored && !saveMutation.isPending
  const canClear = allowed && stored !== null && !saveMutation.isPending

  useEffect(() => {
    if (centeredOnce.current || selected === null) return
    const ruler = rulerRef.current
    const active = ruler?.querySelector<HTMLElement>('[role="option"][aria-selected="true"]')
    if (!ruler || !active) return
    const left = active.offsetLeft - (ruler.clientWidth - active.offsetWidth) / 2
    ruler.scrollTo({ left: Math.max(0, left) })
    centeredOnce.current = true
  }, [selected, slots.length])

  useEffect(() => {
    const ruler = rulerRef.current
    if (!ruler) return
    const update = () => {
      const max = ruler.scrollWidth - ruler.clientWidth
      setOverflowing(max > 2)
      setAtStart(ruler.scrollLeft <= 2)
      setAtEnd(ruler.scrollLeft >= max - 2)
    }
    update()
    ruler.addEventListener('scroll', update, { passive: true })
    const observer = new ResizeObserver(update)
    observer.observe(ruler)
    return () => {
      ruler.removeEventListener('scroll', update)
      observer.disconnect()
    }
  }, [slots.length])

  const scrollStep = (direction: -1 | 1) => {
    const ruler = rulerRef.current
    if (!ruler) return
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ruler.scrollBy({
      left: direction * Math.max(240, Math.round(ruler.clientWidth * 0.72)),
      behavior: reduce ? 'auto' : 'smooth',
    })
  }

  const moveFocus = (index: number, step: number) => {
    const buttons = rulerRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]')
    if (!buttons) return
    let next = index + step
    while (next >= 0 && next < buttons.length) {
      if (!buttons[next].disabled) {
        buttons[next].focus()
        return
      }
      next += step
    }
  }

  return (
    <div className="un-time">
      <header className="un-time__head">
        <Link to="/app/me" className="un-page-back">
          <span aria-hidden="true">←</span> 返回我的
        </Link>
        <h1 className="un-time__title">签到时间</h1>
        <p className="un-time__lede">
          {windowStart && windowEnd
            ? '系统会在 ' + windowStart + '–' + windowEnd + ' 之间自动签到。所选时间只决定开始尝试的先后。'
            : '系统会在有效窗口内自动签到。所选时间只决定开始尝试的先后。'}
        </p>
      </header>

      {error ? (
        <p className="un-alert un-alert--danger" role="alert">
          {error}
        </p>
      ) : null}

      {prefQuery.isPending ? (
        <div className="un-time__skeleton" aria-busy="true">
          <span className="un-skeleton" />
          <span className="un-skeleton" />
        </div>
      ) : prefQuery.isError ? (
        <section className="un-time__panel">
          <p className="un-time__error">暂时读不到当前偏好。为避免覆盖未知值，保存已关闭。</p>
          <button
            type="button"
            className="un-btn un-btn--secondary"
            onClick={() => void prefQuery.refetch()}
          >
            重试
          </button>
        </section>
      ) : (
        <div className="un-time__layout">
          <section className="un-time__main" aria-labelledby="un-time-current">
            <div className="un-time__readout">
              <div>
                <p className="un-time__readout-label">当前选择</p>
                <p id="un-time-current" className="un-time__value un-serif">
                  {selectedSlot?.label ?? '自动分配'}
                </p>
              </div>
              <dl className="un-time__facts">
                <div>
                  <dt>有效窗口</dt>
                  <dd>{windowStart && windowEnd ? windowStart + '–' + windowEnd : '—'}</dd>
                </div>
                <div>
                  <dt>预计执行</dt>
                  <dd>{pref?.estimated ?? '—'}</dd>
                </div>
              </dl>
            </div>

            {!allowed ? (
              <p className="un-alert un-alert--warning">此部署未开放时间偏好；当前值只读。</p>
            ) : null}

            <div className="un-time__ruler-head">
              <h2 className="un-time__section-title">选择时间片</h2>
              <span className="un-time__hint">
                <span className="un-time__hint-desktop">点击或方向键选择</span>
                <span className="un-time__hint-mobile">左右滑动选择</span>
                <span className="un-time__hint-unit">5 分钟一档</span>
                {windowEnd ? (
                  <span className="un-time__hint-end">截止 {windowEnd}</span>
                ) : null}
              </span>
            </div>

            {slots.length === 0 ? (
              <p className="un-time__empty">
                还没有可用时间片；账号审核通过或今天有任务后再来设置。
              </p>
            ) : (
              <>
                <div className="un-time__ruler-shell">
                  {overflowing ? (
                    <button
                      type="button"
                      className="un-time__step is-prev"
                      aria-label="查看更早的时间片"
                      disabled={atStart}
                      onClick={() => scrollStep(-1)}
                    >
                      ←
                    </button>
                  ) : null}
                  <div
                    ref={rulerRef}
                    className="un-time__ruler"
                    data-overflow={overflowing ? 'true' : 'false'}
                    role="listbox"
                    aria-label="签到时间片"
                  >
                    {slots.map((slot, index) => {
                      const isSelected = slot.offset_min === selected
                      const note = slotNote(slot)
                      return (
                        <button
                          key={slot.offset_min}
                          type="button"
                          role="option"
                          aria-selected={isSelected}
                          aria-label={slot.label + (slot.disabled ? '，' + (slot.reason ?? '不可选') : note ? '，' + note : '')}
                          className={
                            'un-time__slot' +
                            (isSelected ? ' is-selected' : '') +
                            (slot.disabled || !allowed ? ' is-disabled' : '') +
                            (slot.pct >= 100 ? ' is-full' : slot.pct >= 80 ? ' is-tight' : '')
                          }
                          disabled={slot.disabled || !allowed}
                          style={{ '--slot-pct': String(Math.min(100, slot.pct)) + '%' } as CSSProperties}
                          onClick={() => {
                            setDraft(slot.offset_min)
                            setError(null)
                          }}
                          onKeyDown={(event) => {
                            if (event.key === 'ArrowRight') {
                              event.preventDefault()
                              moveFocus(index, 1)
                            } else if (event.key === 'ArrowLeft') {
                              event.preventDefault()
                              moveFocus(index, -1)
                            }
                          }}
                        >
                          <span className="un-time__bar" aria-hidden="true" />
                          <span className="un-time__slot-label">{slot.label}</span>
                          {note ? <span className="un-time__slot-note">{note}</span> : null}
                        </button>
                      )
                    })}
                    {windowEnd && !endShown ? (
                      <span className="un-time__end" aria-hidden="true">
                        <span className="un-time__end-tick" />
                        <span className="un-time__end-label">{windowEnd}</span>
                      </span>
                    ) : null}
                  </div>
                  {overflowing ? (
                    <button
                      type="button"
                      className="un-time__step is-next"
                      aria-label="查看更晚的时间片"
                      disabled={atEnd}
                      onClick={() => scrollStep(1)}
                    >
                      →
                    </button>
                  ) : null}
                </div>
                {selectedSlot?.edge_note ? (
                  <p className="un-time__note">{selectedSlot.edge_note}</p>
                ) : null}
                <p className="un-time__note">
                  实际执行会在窗口内按容量微调，不保证精确到分钟。
                </p>
              </>
            )}

            <div className="un-time__actions">
              <button
                type="button"
                className="un-btn un-btn--primary"
                disabled={!canSave}
                onClick={() => {
                  if (draft === null) return
                  setError(null)
                  saveMutation.mutate(draft)
                }}
              >
                {saveMutation.isPending ? '保存中…' : '保存时间偏好'}
              </button>
              <button
                type="button"
                className="un-btn un-btn--ghost"
                disabled={!canClear}
                onClick={() => {
                  setError(null)
                  saveMutation.mutate(null)
                }}
              >
                恢复自动分配
              </button>
            </div>
          </section>

          <aside className="un-time__aside">
            <h2 className="un-time__aside-title">时间怎么生效</h2>
            <p>
              选择的是「开始尝试」的偏好时刻。系统会在有效窗口内按容量调度，
              所以实际执行可能早于或晚于这个时刻。
            </p>
            <p>
              标记“紧张”或“满员”的档位仍可保存，但高峰时段可能顺延。
              审核中的账号会暂时没有可选项。
            </p>
            <p className="un-time__aside-quiet">
              时间以学校配置的时区为准；当前页面显示的是上海时间。
            </p>
          </aside>
        </div>
      )}
    </div>
  )
}
